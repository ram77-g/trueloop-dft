"""The TrustLoop service: REST API, MCP server for Talkdesk, agent card and console.

Run with:  uvicorn trustloop.app:create_app --factory --host 0.0.0.0 --port 8000
"""

import html
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import bank, clock, db, mender, promises
from .config import Settings, load_settings
from .llm import get_llm
from .mcp_tools import build_mcp

log = logging.getLogger("trustloop")
STATIC = Path(__file__).resolve().parent / "static"
PROTECTED = ("/api", "/mcp", "/sse", "/messages")


class TranscriptIn(BaseModel):
    text: str
    agent_name: str = "Agent"
    agent_type: str = "human"
    channel: str = "chat"
    phone: str = ""
    customer_id: str = ""
    session_id: str = ""
    started_at: str = ""


class ClockIn(BaseModel):
    advance_hours: float = 0
    reset: bool = False


class EventIn(BaseModel):
    customer_id: str
    kind: str


class NoteIn(BaseModel):
    note: str = ""


class InstructionsIn(BaseModel):
    text: str
    label: str = "edited in console"


class PatchIn(BaseModel):
    kind: str = "fix"


class ResultsIn(BaseModel):
    before: dict[str, bool]
    after: dict[str, bool]


def _webhook_text(payload: dict) -> tuple[str, str]:
    """Accept the shapes a Talkdesk automation might send: plain text, or a list of messages."""
    phone = str(payload.get("phone") or payload.get("contact_phone") or payload.get("customer_phone") or "")
    text = payload.get("text") or payload.get("transcript") or ""
    if not text and isinstance(payload.get("messages"), list):
        lines = []
        for m in payload["messages"]:
            who = m.get("role") or m.get("sender") or m.get("author") or m.get("from") or "Customer"
            what = m.get("text") or m.get("content") or m.get("body") or ""
            who = "Agent" if str(who).lower() in {"agent", "assistant", "ai", "bot", "virtual_agent"} else str(who)
            lines.append(f"{who}: {what}")
        text = "\n".join(lines)
    return str(text), phone


def create_app(settings: Settings | None = None, store: db.Store | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = store or db.Store(settings.db_path)
    bank.seed(store)
    mender.ensure_v1(store)
    db.set_default_store(store)
    llm = get_llm(settings)
    if llm is not None and hasattr(llm, "candidates"):
        # Find a Gemini model that's answering now, so the first real request doesn't wait on outages.
        threading.Thread(target=lambda: llm.candidates, name="gemini-warmup", daemon=True).start()
    mcp = build_mcp()
    mcp_http = mcp.streamable_http_app()
    mcp_sse = mcp.sse_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="TrustLoop", version="1.0", lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    app.router.routes.extend(mcp_http.routes)
    app.router.routes.extend(mcp_sse.routes)

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        if settings.api_key and request.url.path.startswith(PROTECTED):
            auth = request.headers.get("authorization", "")
            given = (auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else "") \
                or request.headers.get("x-api-key", "") or request.query_params.get("key", "")
            if given != settings.api_key:
                return JSONResponse({"detail": "Missing or wrong API key"}, status_code=401)
        return await call_next(request)

    # --- health and console ---------------------------------------------------------------

    @app.get("/health")
    def health():
        return {"ok": True, "llm": settings.llm_mode, "model": llm.model if llm else None,
                "now": clock.iso(clock.now(store)), "clock_offset_hours": clock.offset_hours(store),
                "webhook": bool(settings.webhook_url), "auth": bool(settings.api_key)}

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(STATIC / "index.html")

    # --- Promise Keeper -------------------------------------------------------------------

    @app.get("/api/customers")
    def customers():
        return store.query("SELECT customer_id, name, phone, email FROM customers ORDER BY customer_id")

    @app.post("/api/transcripts")
    def add_transcript(body: TranscriptIn):
        if not body.text.strip():
            raise HTTPException(400, "The transcript is empty")
        result = promises.ingest_transcript(store, llm, source="api", **body.model_dump())
        promises.sweep(store, settings)
        return result

    @app.post("/api/webhooks/talkdesk")
    def talkdesk_webhook(payload: dict = Body(...)):
        text, phone = _webhook_text(payload)
        if not text.strip():
            raise HTTPException(400, "No transcript text found in the payload")
        agent_type = "ai" if str(payload.get("agent_type", "")).lower() in {"ai", "virtual", "autopilot"} else "human"
        return promises.ingest_transcript(
            store, llm, text=text, phone=phone, agent_type=agent_type, source="talkdesk_webhook",
            agent_name=str(payload.get("agent_name") or ("CardCare" if agent_type == "ai" else "Agent")),
            session_id=str(payload.get("interaction_id") or payload.get("session_id") or ""),
            channel=str(payload.get("channel") or "chat"))

    @app.get("/api/transcripts")
    def transcripts():
        return store.query("SELECT * FROM transcripts ORDER BY id DESC LIMIT 100")

    @app.get("/api/promises")
    def list_promises(status: str = "", customer_id: str = "", agent_type: str = ""):
        promises.sweep(store, settings)
        return promises.list_promises(store, status=status, customer_id=customer_id, agent_type=agent_type)

    @app.post("/api/promises/{promise_id}/kept")
    def keep(promise_id: int, body: NoteIn):
        row = promises.mark_kept(store, promise_id, body.note)
        if not row:
            raise HTTPException(404, "No such promise")
        promises.sweep(store, settings)
        return row

    @app.post("/api/promises/{promise_id}/dismiss")
    def dismiss(promise_id: int):
        row = promises.dismiss(store, promise_id)
        if not row:
            raise HTTPException(404, "No such promise")
        return row

    @app.get("/api/dashboard")
    def dashboard():
        return promises.dashboard(store, settings)

    @app.get("/api/card")
    def agent_card(phone: str = "", customer_id: str = ""):
        result = promises.card(store, settings, phone=phone, customer_id=customer_id)
        if not result:
            raise HTTPException(404, "No customer with that phone number")
        return result

    @app.get("/card", response_class=HTMLResponse)
    def agent_card_html(phone: str = "", customer_id: str = ""):
        # Left outside the API key so Workspace Designer can embed it without custom headers.
        result = promises.card(store, settings, phone=phone, customer_id=customer_id)
        return HTMLResponse(_card_html(result, phone))

    @app.get("/api/messages")
    def messages():
        promises.sweep(store, settings)
        return store.query("SELECT * FROM messages ORDER BY id DESC LIMIT 200")

    @app.get("/api/clock")
    def get_clock():
        return {"now": clock.iso(clock.now(store)), "offset_hours": clock.offset_hours(store)}

    @app.post("/api/clock")
    def move_clock(body: ClockIn):
        if body.reset:
            clock.reset(store)
        elif body.advance_hours:
            clock.advance(store, body.advance_hours)
        swept = promises.sweep(store, settings)
        return {"now": clock.iso(clock.now(store)), "offset_hours": clock.offset_hours(store), "sweep": swept}

    @app.post("/api/events")
    def bank_event(body: EventIn):
        try:
            event = bank.simulate(store, body.customer_id, body.kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"event": event, "sweep": promises.sweep(store, settings)}

    @app.get("/api/tool-calls")
    def tool_calls(limit: int = 100):
        return store.query("SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    # --- Mender ---------------------------------------------------------------------------

    @app.post("/api/mender/simulate-traffic")
    def simulate_traffic():
        return mender.simulate_traffic(store)

    @app.post("/api/mender/capture")
    def capture():
        result = mender.capture(store, llm)
        return {"new_failures": result["new_failures"], "clusters": mender.clusters(store)}

    @app.get("/api/mender/failures")
    def failures():
        return mender.list_failures(store)

    @app.get("/api/mender/clusters")
    def clusters():
        return mender.clusters(store)

    @app.post("/api/mender/cases")
    def build_cases():
        return mender.build_cases(store)

    @app.get("/api/mender/cases")
    def get_cases():
        return mender.get_cases(store)

    @app.get("/api/mender/cases.csv", response_class=PlainTextResponse)
    def cases_csv():
        return PlainTextResponse(mender.cases_csv(mender.get_cases(store)), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=trustloop_cases.csv"})

    @app.get("/api/mender/instructions")
    def instructions():
        mender.ensure_v1(store)
        return store.query("SELECT * FROM instructions ORDER BY version DESC")

    @app.put("/api/mender/instructions")
    def edit_instructions(body: InstructionsIn):
        return mender.set_instructions(store, body.text, body.label)

    @app.post("/api/mender/patches")
    def propose(body: PatchIn):
        try:
            if body.kind == "overeager":
                return mender.propose_overeager_patch(store)
            return mender.propose_patch(store, llm)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/mender/patches")
    def patches():
        return mender.list_patches(store)

    @app.get("/api/mender/patches/{patch_id}")
    def patch(patch_id: int):
        row = mender.get_patch(store, patch_id)
        if not row:
            raise HTTPException(404, "No such patch")
        return row

    @app.post("/api/mender/patches/{patch_id}/evaluate")
    def evaluate(patch_id: int):
        try:
            return mender.evaluate_patch(store, llm, patch_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/mender/patches/{patch_id}/external-results")
    def external_results(patch_id: int, body: ResultsIn):
        if not mender.get_patch(store, patch_id):
            raise HTTPException(404, "No such patch")
        return mender.record_external_results(store, patch_id, body.before, body.after)

    @app.post("/api/mender/patches/{patch_id}/approve")
    def approve(patch_id: int):
        try:
            return mender.approve(store, patch_id)
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- demo helpers ---------------------------------------------------------------------

    @app.post("/api/demo/reset")
    def reset_demo():
        tables = ["tool_calls", "outcomes", "transcripts", "promises", "messages", "bank_events", "failures",
                  "patches", "eval_runs", "instructions", "declines", "cards", "customers", "settings"]
        with store.transaction():
            for table in tables:
                store.execute(f"DELETE FROM {table}")
        bank.seed(store)
        mender.ensure_v1(store)
        return {"reset": True, "now": clock.iso(clock.now(store))}

    @app.post("/api/demo/load-samples")
    def load_samples():
        folder = Path(__file__).resolve().parent.parent / "demo" / "transcripts"
        loaded = []
        for path, kwargs in (
            ("human_fatima_dispute.txt", {"agent_name": "Ravi", "phone": "+919845001009"}),
            ("ai_meera_replacement.txt", {"agent_name": "CardCare", "agent_type": "ai", "phone": "+919845001005"}),
        ):
            result = promises.ingest_transcript(store, llm, text=(folder / path).read_text(encoding="utf-8"),
                                                source="sample", **kwargs)
            loaded.append({"file": path, "promises": len(result["promises"]), "engine": result["engine"]})
        return loaded

    return app


def _card_html(card: dict | None, phone: str) -> str:
    esc = html.escape
    if not card:
        body = f"<p class='muted'>No Nimbus Bank customer found for {esc(phone or 'this contact')}.</p>"
    else:
        def items(rows, cls):
            return "".join(
                f"<li class='{cls}'><b>{esc(r['description'])}</b><br><span class='muted'>due "
                f"{esc(r['due_at'][:16].replace('T', ' '))} · promised by {esc(r['made_by'])}"
                f"{' · ' + esc(r['policy_violation']) if r['policy_violation'] else ''}</span></li>"
                for r in rows)
        tone = "bad" if card["broken"] else ("warn" if card["cant_keep"] else "ok")
        body = (f"<div class='head {tone}'>{esc(card['headline'])}</div>"
                f"<ul>{items(card['broken'], 'bad')}{items(card['cant_keep'], 'warn')}{items(card['open'], 'ok')}</ul>"
                f"<p class='muted'>{esc(card['customer']['name'])} · {esc(card['customer']['phone'])}</p>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>TrustLoop promises</title><style>
body{{font:14px/1.4 system-ui,sans-serif;margin:12px;color:#1d1b29;background:#fff}}
.head{{padding:10px 12px;border-radius:8px;font-weight:600;margin-bottom:8px}}
.bad{{background:#fdecec;color:#8a1c1c}}.warn{{background:#fff4e0;color:#7a4b00}}.ok{{background:#eef6ee;color:#1f5d2a}}
ul{{list-style:none;padding:0;margin:0}}li{{padding:8px 10px;border-radius:8px;margin:6px 0}}
.muted{{color:#5f5b72;font-weight:400;font-size:12px}}
</style></head><body><div class="muted">TrustLoop · promises to this customer</div>{body}</body></html>"""

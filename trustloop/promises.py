"""Promise Keeper: find promises in conversations, check them against policy, track and follow through."""

import logging
import re
from datetime import datetime, timedelta

import httpx

from . import bank, clock
from .config import Settings
from .dates import add_working_days, resolve
from .db import Store
from .llm import LLM, LLMError
from .policy import PROMISE_KINDS, check_promise, policy_text

log = logging.getLogger("trustloop.promises")

AGENT_SPEAKERS = {"agent", "ai", "bot", "assistant", "cardcare", "autopilot", "virtual agent", "advisor", "support"}
KEPT_BY_EVENT = {
    "replacement": "replacement_delivered",
    "refund": "refund_paid",
    "callback": "callback_completed",
    "document": "document_sent",
    "international": "international_enabled",
}
DUE_SOON = timedelta(hours=24)

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "promises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "kind": {"type": "string", "enum": PROMISE_KINDS},
                    "description": {"type": "string"},
                    "due_phrase": {"type": "string"},
                    "due_at": {"type": "string"},
                },
                "required": ["quote", "kind", "description", "due_phrase", "due_at"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["promises"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM = """You find commitments that a contact-centre agent (a person or an AI agent) makes to a customer.

A commitment is something the agent or the company says WILL happen for the customer: a callback, a refund or reversal, an email or document, a card delivery, a change to the account, or any follow-up. Ignore questions, conditions the customer must meet ("if you upload the form..."), general information, and anything the customer promises.

For each commitment return:
- quote: the agent's exact words that make the commitment (copy them verbatim).
- kind: callback, refund, document, replacement, international or follow_up.
- description: a short plain description of what was promised, written for a supervisor (e.g. "Call Fatima back about the disputed Uber charge").
- due_phrase: the exact words that say when ("on Thursday", "in 2 days", "within 5-7 working days"), or "" if no time was given.
- due_at: your best ISO 8601 date-time for the deadline in IST (+05:30), using the conversation time given, or "" if no time was given.

If the agent makes two commitments in one sentence, return them separately. Return an empty list when there are none."""

COMMITMENT = re.compile(
    r"\b(i'll|i will|we'll|we will|i am going to|i'm going to|i'll make sure|we'll make sure|"
    r"you'll (?:receive|get|have)|you will (?:receive|get|have)|"
    r"(?:will|should) (?:arrive|reach|be (?:delivered|credited|refunded|reversed|sent|done|processed)))\b",
    re.IGNORECASE,
)
KIND_PATTERNS = [
    ("callback", r"call (?:you )?back|callback|call you|ring you|get back to you"),
    ("refund", r"refund|revers|credited|money back"),
    ("document", r"e-?mail|send you|form|document|statement|letter"),
    ("replacement", r"new card|replacement|card (?:will |should )?(?:arrive|reach)|deliver"),
    ("international", r"international|abroad"),
]


def parse_transcript(text: str, agent_name: str = "") -> list[dict]:
    """Split "Speaker: words" lines into turns with a role of agent or customer."""
    agent_names = AGENT_SPEAKERS | ({agent_name.lower()} if agent_name else set())
    turns: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(
            r"^(?:\[?\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\]?\s*)?([A-Za-z][\w .'-]{0,40}?)\s*:\s*(.+)$", line
        )
        if match:
            speaker = match.group(1).strip()
            role = "agent" if speaker.lower() in agent_names or speaker.lower().startswith("agent") else "customer"
            turns.append({"speaker": speaker, "role": role, "text": match.group(2).strip()})
        elif turns:
            turns[-1]["text"] += " " + line
    return turns


def _kinds_in(text: str) -> list[str]:
    found = []
    for kind, pattern in KIND_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            found.append((m.start(), kind))
    return [k for _, k in sorted(found)]


def extract_offline(text: str, agent_name: str = "") -> list[dict]:
    """Rule-based extraction, used without an API key and as the fallback."""
    candidates = []
    for turn in parse_transcript(text, agent_name):
        if turn["role"] != "agent":
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", turn["text"]):
            if sentence.rstrip().endswith("?") or not COMMITMENT.search(sentence):
                continue
            if re.match(r"\s*if you\b", sentence, re.IGNORECASE):
                continue
            kinds = _kinds_in(sentence) or ["follow_up"]
            parts = re.split(r"\s+and\s+(?=(?:i'll|i will|we'll|we will|also|then)?\s*\w)", sentence) if len(kinds) > 1 else [sentence]
            for part in parts:
                part_kinds = _kinds_in(part)
                if len(kinds) > 1 and not part_kinds:
                    continue
                kind = (part_kinds or kinds)[0]
                candidates.append({
                    "quote": sentence.strip(),
                    "kind": kind,
                    "description": part.strip().rstrip("."),
                    "due_phrase": _due_phrase(part),
                    "due_at": "",
                })
    return candidates


def _due_phrase(text: str) -> str:
    base = datetime(2026, 1, 5, 9, 0, tzinfo=clock.IST)  # any base: only the matched words are kept
    return resolve(text, base)[1]


def extract_with_llm(llm: LLM, text: str, created_at: datetime, agent_name: str) -> list[dict]:
    prompt = (
        f"Conversation time: {created_at:%A %d %B %Y, %H:%M} IST\n"
        f"Agent: {agent_name}\n\nBank policy for reference:\n{policy_text()}\n\n"
        f"Transcript:\n{text}"
    )
    return llm.json(system=EXTRACTION_SYSTEM, prompt=prompt, schema=EXTRACTION_SCHEMA)["promises"]


def default_due(kind: str, created_at: datetime) -> datetime:
    if kind in ("refund", "replacement"):
        return add_working_days(created_at, 7).replace(hour=18, minute=0, second=0)
    if kind in ("callback", "follow_up"):
        return add_working_days(created_at, 1).replace(hour=18, minute=0, second=0)
    return (created_at + timedelta(days=1)).replace(hour=18, minute=0, second=0)


def _resolve_due(candidate: dict, created_at: datetime) -> tuple[datetime, str, bool]:
    for text in (candidate.get("due_phrase", ""), candidate.get("quote", "")):
        if text:
            due, phrase = resolve(text, created_at)
            if due:
                return due, phrase, False
    if candidate.get("due_at"):
        try:
            return clock.parse(candidate["due_at"]), candidate.get("due_phrase", ""), False
        except ValueError:
            pass
    return default_due(candidate["kind"], created_at), "", True


def save_promise(store: Store, candidate: dict, *, created_at: datetime, session_id: str, customer_id: str | None,
                 made_by: str, agent_type: str, source: str, transcript_id: int | None = None) -> dict:
    kind = candidate["kind"] if candidate["kind"] in PROMISE_KINDS else "follow_up"
    due, phrase, no_date = _resolve_due({**candidate, "kind": kind}, created_at)
    violation = check_promise(kind, created_at, due)
    promise_id = store.execute(
        "INSERT INTO promises (created_at, session_id, transcript_id, customer_id, made_by, agent_type, quote, kind, "
        "description, due_at, due_phrase, no_date, policy_violation, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (clock.iso(created_at), session_id, transcript_id, customer_id, made_by, agent_type,
         candidate["quote"], kind, candidate.get("description") or candidate["quote"], clock.iso(due),
         phrase, int(no_date), violation, source),
    )
    return get_promise(store, promise_id)


def ingest_transcript(store: Store, llm: LLM | None, *, text: str, agent_name: str, agent_type: str = "human",
                      channel: str = "chat", phone: str = "", customer_id: str = "", session_id: str = "",
                      started_at: str = "", source: str = "console") -> dict:
    customer = bank.find_customer(store, phone=phone, customer_id=customer_id)
    created_at = clock.parse(started_at) if started_at else clock.now(store)
    session_id = session_id or f"tx-{int(created_at.timestamp())}-{(customer or {}).get('customer_id', 'anon')}"
    transcript_id = store.execute(
        "INSERT INTO transcripts (at, session_id, customer_id, agent_name, agent_type, channel, text, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (clock.iso(created_at), session_id, customer and customer["customer_id"], agent_name, agent_type,
         channel, text, source),
    )
    engine = "offline"
    candidates = None
    if llm:
        try:
            candidates = extract_with_llm(llm, text, created_at, agent_name)
            engine = llm.name
        except (LLMError, Exception) as exc:  # the demo must survive an API hiccup
            log.warning("%s extraction failed, using the offline extractor: %s", llm.name, exc)
    if candidates is None:
        candidates = extract_offline(text, agent_name)
    saved = [
        save_promise(store, c, created_at=created_at, session_id=session_id,
                     customer_id=customer and customer["customer_id"], made_by=agent_name,
                     agent_type=agent_type, source=engine, transcript_id=transcript_id)
        for c in candidates
    ]
    return {"transcript_id": transcript_id, "session_id": session_id, "engine": engine,
            "customer": customer, "promises": saved}


def status_of(row: dict, now: datetime) -> str:
    if row["dismissed"]:
        return "dismissed"
    if row["kept_at"]:
        return "kept"
    due = clock.parse(row["due_at"])
    if now > due:
        return "broken"
    if row["policy_violation"]:
        return "cant_keep"
    if due - now <= DUE_SOON:
        return "due_soon"
    return "open"


def _decorate(store: Store, row: dict, now: datetime) -> dict:
    row["status"] = status_of(row, now)
    customer = bank.find_customer(store, customer_id=row["customer_id"]) if row["customer_id"] else None
    row["customer_name"] = customer["name"] if customer else ""
    row["no_date"] = bool(row["no_date"])
    row["dismissed"] = bool(row["dismissed"])
    return row


def get_promise(store: Store, promise_id: int) -> dict | None:
    row = store.query_one("SELECT * FROM promises WHERE id = ?", (promise_id,))
    return _decorate(store, row, clock.now(store)) if row else None


def list_promises(store: Store, status: str = "", customer_id: str = "", agent_type: str = "") -> list[dict]:
    now = clock.now(store)
    sql, params = "SELECT * FROM promises WHERE 1=1", []
    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    if agent_type:
        sql += " AND agent_type = ?"
        params.append(agent_type)
    rows = [_decorate(store, r, now) for r in store.query(sql + " ORDER BY due_at", params)]
    return [r for r in rows if not status or r["status"] == status]


def mark_kept(store: Store, promise_id: int, note: str = "") -> dict | None:
    store.execute("UPDATE promises SET kept_at = ?, kept_note = ? WHERE id = ? AND kept_at IS NULL",
                  (clock.iso(clock.now(store)), note or "Marked done by the agent", promise_id))
    return get_promise(store, promise_id)


def dismiss(store: Store, promise_id: int) -> dict | None:
    store.execute("UPDATE promises SET dismissed = 1 WHERE id = ?", (promise_id,))
    return get_promise(store, promise_id)


def auto_keep(store: Store) -> int:
    """Mark promises kept when the bank reports the matching event after the promise was made."""
    kept = 0
    for row in store.query("SELECT * FROM promises WHERE kept_at IS NULL AND dismissed = 0 AND customer_id IS NOT NULL"):
        event_kind = KEPT_BY_EVENT.get(row["kind"])
        if not event_kind:
            continue
        event = store.query_one(
            "SELECT * FROM bank_events WHERE customer_id = ? AND kind = ? AND at >= ? ORDER BY at LIMIT 1",
            (row["customer_id"], event_kind, row["created_at"]),
        )
        if event:
            late = clock.parse(event["at"]) > clock.parse(row["due_at"])
            store.execute("UPDATE promises SET kept_at = ?, kept_note = ? WHERE id = ?",
                          (event["at"], ("Kept late: " if late else "Kept: ") + event["detail"], row["id"]))
            kept += 1
    return kept


def _deliver(store: Store, settings: Settings, row: dict, audience: str, purpose: str, body: str) -> None:
    exists = store.query_one("SELECT id FROM messages WHERE promise_id = ? AND purpose = ?", (row["id"], purpose))
    if exists:
        return
    status = "queued"
    if settings.webhook_url and audience == "customer":
        customer = bank.find_customer(store, customer_id=row["customer_id"]) if row["customer_id"] else None
        try:
            httpx.post(settings.webhook_url, timeout=5.0, json={
                "purpose": purpose, "body": body, "promise_id": row["id"],
                "customer_id": row["customer_id"],
                "phone": customer and customer["phone"], "email": customer and customer["email"],
            }).raise_for_status()
            status = "sent"
        except httpx.HTTPError as exc:
            log.warning("Webhook delivery failed: %s", exc)
            status = "failed"
    store.execute(
        "INSERT OR IGNORE INTO messages (at, promise_id, customer_id, audience, purpose, body, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), row["id"], row["customer_id"], audience, purpose, body, status),
    )


def sweep(store: Store, settings: Settings) -> dict:
    """Keep promises current: mark kept ones, then queue reminders, alerts and customer messages once each."""
    kept = auto_keep(store)
    counts: dict[str, int] = {}
    for row in list_promises(store):
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        due = clock.parse(row["due_at"])
        name = row["customer_name"] or "the customer"
        if status == "due_soon":
            _deliver(store, settings, row, "agent", "reminder",
                     f"Reminder for {row['made_by']}: you promised {name} \"{row['description']}\" by {due:%a %d %b, %H:%M}.")
        elif status == "broken":
            _deliver(store, settings, row, "agent", "broken_alert",
                     f"Broken promise: {row['made_by']} promised {name} \"{row['description']}\" by {due:%a %d %b, %H:%M}.")
            _deliver(store, settings, row, "customer", "apology",
                     f"Hi {name.split()[0] if row['customer_name'] else ''}, we promised: {row['description']} by "
                     f"{due:%A}. We haven't done it yet, and we're sorry. Someone from Nimbus Bank will contact you today.")
        elif status == "kept":
            _deliver(store, settings, row, "customer", "kept_confirmation",
                     f"Done as promised: {row['description']}. Thank you for banking with Nimbus Bank.")
        if row["policy_violation"] and status in ("cant_keep", "broken"):
            _deliver(store, settings, row, "supervisor", "cant_keep_alert",
                     f"{row['made_by']} made a promise policy doesn't allow: \"{row['quote']}\". {row['policy_violation']}")
    return {"kept_now": kept, "counts": counts}


def _when(due: datetime, now: datetime) -> str:
    if due.date() == now.date():
        return f"today at {due:%H:%M}"
    if abs((due.date() - now.date()).days) < 7:
        return f"{due:%A} at {due:%H:%M}" if due > now else f"on {due:%A}"
    return f"on {due:%d %b}"


def card(store: Store, settings: Settings, phone: str = "", customer_id: str = "") -> dict | None:
    """What the agent sees when this customer gets in touch."""
    customer = bank.find_customer(store, phone=phone, customer_id=customer_id)
    if not customer:
        return None
    sweep(store, settings)
    now = clock.now(store)
    rows = list_promises(store, customer_id=customer["customer_id"])
    groups = {s: [r for r in rows if r["status"] == s] for s in ("broken", "cant_keep", "due_soon", "open", "kept")}
    first = customer["name"].split()[0]
    if groups["broken"]:
        p = groups["broken"][0]
        headline = (f"You promised {first} \"{p['description']}\" (due {clock.parse(p['due_at']):%a %d %b, %H:%M}). "
                    "It hasn't happened. Apologise first.")
    elif groups["cant_keep"]:
        p = groups["cant_keep"][0]
        headline = f"{first} was given a promise policy doesn't allow: {p['policy_violation']} Set expectations now."
    elif groups["due_soon"] or groups["open"]:
        p = (groups["due_soon"] or groups["open"])[0]
        headline = f"Open promise to {first}: \"{p['description']}\", due {_when(clock.parse(p['due_at']), now)}."
    else:
        headline = f"No open promises to {first}."
    return {
        "customer": {k: customer[k] for k in ("customer_id", "name", "phone", "email")},
        "headline": headline,
        "broken": groups["broken"],
        "cant_keep": groups["cant_keep"],
        "open": groups["due_soon"] + groups["open"],
        "kept": groups["kept"][-5:],
        "as_of": clock.iso(now),
    }


def dashboard(store: Store, settings: Settings) -> dict:
    sweep(store, settings)
    rows = list_promises(store)
    totals: dict[str, int] = {}
    by_agent: dict[str, dict[str, int]] = {}
    for r in rows:
        totals[r["status"]] = totals.get(r["status"], 0) + 1
        agent = by_agent.setdefault(r["made_by"], {"agent_type": r["agent_type"]})
        agent[r["status"]] = agent.get(r["status"], 0) + 1
    repeat_contacts = 0
    for r in rows:
        if r["status"] == "broken" and r["customer_id"]:
            later = store.query_one(
                "SELECT COUNT(*) AS n FROM transcripts WHERE customer_id = ? AND at > ?",
                (r["customer_id"], r["due_at"]),
            )
            repeat_contacts += later["n"]
    return {"totals": totals, "by_agent": by_agent, "repeat_contacts_after_broken": repeat_contacts,
            "total": len(rows), "as_of": clock.iso(clock.now(store)), "clock_offset_hours": clock.offset_hours(store)}

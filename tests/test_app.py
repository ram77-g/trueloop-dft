import asyncio
import json
import logging
import socket
import threading
import time
from dataclasses import replace

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from trustloop import db
from trustloop.app import create_app
from trustloop.config import load_settings

HUMAN = ("Agent: I'll call you back on Thursday, and I'll email you the dispute form today.\n"
         "Customer: Thanks.")


def _app(api_key: str = ""):
    settings = replace(load_settings(), db_path=":memory:", api_key=api_key)
    return create_app(settings, db.Store(":memory:"))


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


def test_health_and_console(client):
    health = client.get("/health").json()
    assert health["ok"] and health["llm"] == "offline"
    assert "TrustLoop" in client.get("/").text


def test_promise_flow_over_http(client):
    result = client.post("/api/transcripts", json={"text": HUMAN, "agent_name": "Ravi", "phone": "+919845001009"}).json()
    assert len(result["promises"]) == 2
    card = client.get("/api/card", params={"phone": "9845001009"}).json()
    assert card["customer"]["name"] == "Fatima Sheikh" and len(card["open"]) == 2
    client.post("/api/clock", json={"advance_hours": 24 * 8})
    card = client.get("/api/card", params={"phone": "9845001009"}).json()
    assert len(card["broken"]) == 2 and "It hasn't happened" in card["headline"]
    assert "It hasn&#x27;t happened" in client.get("/card", params={"phone": "9845001009"}).text
    board = client.get("/api/dashboard").json()
    assert board["totals"]["broken"] == 2
    purposes = {m["purpose"] for m in client.get("/api/messages").json()}
    # A reminder may also exist: the "email the form today" promise is due within 24 hours when it's made.
    assert {"apology", "broken_alert"} <= purposes <= {"apology", "broken_alert", "reminder"}
    client.post("/api/clock", json={"reset": True})
    assert client.get("/api/clock").json()["offset_hours"] == 0


def test_kept_by_bank_event_and_by_agent(client):
    created = client.post("/api/transcripts", json={"text": HUMAN, "agent_name": "Ravi", "phone": "+919845001009"}).json()
    callback, document = sorted(created["promises"], key=lambda p: p["kind"])
    client.post("/api/events", json={"customer_id": "C009", "kind": "callback_completed"})
    assert client.get("/api/promises", params={"status": "kept"}).json()[0]["id"] == callback["id"]
    assert client.post(f"/api/promises/{document['id']}/kept", json={"note": "emailed"}).json()["status"] == "kept"
    assert client.post("/api/events", json={"customer_id": "C009", "kind": "nope"}).status_code == 400


def test_validation_errors(client):
    assert client.post("/api/transcripts", json={"text": "  "}).status_code == 400
    assert client.get("/api/card", params={"phone": "123"}).status_code == 404
    assert client.post("/api/promises/999/kept", json={}).status_code == 404
    assert client.post("/api/mender/patches", json={"kind": "fix"}).status_code == 400  # nothing captured yet


def test_talkdesk_webhook_message_list(client):
    payload = {"contact_phone": "+919845001002", "interaction_id": "td-123", "agent_name": "Asha",
               "messages": [{"role": "customer", "text": "Where's my refund?"},
                            {"role": "agent", "text": "Your refund will be credited within 5 working days."}]}
    result = client.post("/api/webhooks/talkdesk", json=payload).json()
    assert result["session_id"] == "td-123"
    assert [p["kind"] for p in result["promises"]] == ["refund"]


def test_mender_flow_over_http(client):
    assert len(client.post("/api/mender/simulate-traffic").json()) == 5
    clusters = client.post("/api/mender/capture").json()["clusters"]
    assert len(clusters) == 5
    assert len(client.post("/api/mender/cases").json()) == 11
    assert client.get("/api/mender/cases.csv").text.startswith("case_id,kind")
    fix = client.post("/api/mender/patches", json={"kind": "fix"}).json()
    report = client.post(f"/api/mender/patches/{fix['id']}/evaluate").json()
    assert report["gate"]["decision"] == "PASS"
    bad = client.post("/api/mender/patches", json={"kind": "overeager"}).json()
    assert client.post(f"/api/mender/patches/{bad['id']}/evaluate").json()["gate"]["decision"] == "BLOCK"
    assert client.post(f"/api/mender/patches/{bad['id']}/approve").status_code == 409
    v2 = client.post(f"/api/mender/patches/{fix['id']}/approve").json()
    assert v2["version"] == 2
    assert client.get("/api/mender/instructions").json()[0]["version"] == 2


def test_demo_reset_and_samples(client):
    loaded = client.post("/api/demo/load-samples").json()
    assert [x["promises"] for x in loaded] == [2, 1]
    client.post("/api/demo/reset")
    assert client.get("/api/promises").json() == []
    assert len(client.get("/api/customers").json()) == 16


def test_api_key_is_enforced():
    with TestClient(_app(api_key="s3cret")) as c:
        assert c.get("/api/customers").status_code == 401
        assert c.get("/api/customers", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/customers", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/api/customers", params={"key": "s3cret"}).status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        assert c.get("/health").status_code == 200
        assert c.get("/card", params={"phone": "9845001009"}).status_code == 200


# --- a real server, reached with the official MCP client as Talkdesk would -----------------------

LIVE_KEY = "live-test-key"
AUTH = {"Authorization": f"Bearer {LIVE_KEY}"}


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


SERVER_ERRORS = _Collect()


@pytest.fixture(scope="module")
def live_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_app(api_key=LIVE_KEY), host="127.0.0.1", port=port, log_level="warning"))
    logging.getLogger("uvicorn.error").addHandler(SERVER_ERRORS)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


async def _exercise(session: ClientSession) -> tuple[set, str]:
    await session.initialize()
    names = {t.name for t in (await session.list_tools()).tools}
    result = await session.call_tool("verify_customer",
                                     {"phone": "+919845001005", "last4": "6604", "dob": "1995-05-09"})
    return names, json.dumps([c.model_dump() for c in result.content])


def test_mcp_over_streamable_http(live_url):
    async def run():
        async with streamablehttp_client(f"{live_url}/mcp", headers=AUTH) as (read, write, _):
            async with ClientSession(read, write) as session:
                return await _exercise(session)
    names, payload = asyncio.run(run())
    assert {"verify_customer", "record_promise", "get_customer_promises"} <= names
    assert "Meera Nair" in payload


def test_mcp_over_sse(live_url):
    async def run():
        async with sse_client(f"{live_url}/sse", headers=AUTH) as (read, write):
            async with ClientSession(read, write) as session:
                return await _exercise(session)
    names, payload = asyncio.run(run())
    assert len(names) == 9 and "Meera Nair" in payload


def test_live_server_rejects_missing_key(live_url):
    import httpx
    assert httpx.get(f"{live_url}/api/customers").status_code == 401


def test_streaming_endpoints_raise_no_server_errors(live_url):
    """SSE and streaming responses must pass through the auth middleware without ASGI errors."""
    time.sleep(0.5)  # let the SSE disconnect from the earlier test finish on the server
    errors = [r.getMessage() for r in SERVER_ERRORS.records]
    assert errors == [], errors

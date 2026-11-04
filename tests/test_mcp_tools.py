import asyncio
import json

from trustloop import mcp_tools, promises


def test_server_lists_every_tool_with_schemas(store):
    server = mcp_tools.build_mcp()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == set(mcp_tools.TOOLS)
    verify = tools["verify_customer"].inputSchema
    assert set(verify["required"]) == {"phone", "last4", "dob"}
    assert "session_id" in verify["properties"]
    assert "date of birth" in tools["verify_customer"].description


def test_server_call_tool_round_trip(store):
    server = mcp_tools.build_mcp()
    result = asyncio.run(server.call_tool("verify_customer", {
        "phone": "+919845001005", "last4": "6604", "dob": "1995-05-09", "session_id": "s1"}))
    text = json.dumps(result, default=str)
    assert "Meera Nair" in text


def test_talkdesk_style_string_arguments(store):
    """Talkdesk passes every tool parameter as a string, booleans as 'true'/'false'."""
    server = mcp_tools.build_mcp()
    asyncio.run(server.call_tool("set_international_usage",
                                 {"phone": "+919845001004", "enabled": "true", "days": "30", "session_id": "td-1"}))
    asyncio.run(server.call_tool("record_outcome", {"intent": "card_declined", "resolved": "false",
                                                     "escalated": "true", "phone": "+919845001002",
                                                     "session_id": "td-2"}))
    card = store.query_one("SELECT international FROM cards WHERE customer_id = 'C004'")
    outcome = store.query_one("SELECT resolved, escalated FROM outcomes WHERE session_id = 'td-2'")
    assert card["international"] == 1 and (outcome["resolved"], outcome["escalated"]) == (0, 1)


def test_calls_are_logged_per_session(store):
    mcp_tools.verify_customer("+919845001005", "6604", "1995-05-09", session_id="s-42")
    mcp_tools.block_card("+919845001005", "stolen", session_id="s-42")
    rows = store.query("SELECT tool, customer_id FROM tool_calls WHERE session_id = 's-42' ORDER BY id")
    assert [(r["tool"], r["customer_id"]) for r in rows] == [("verify_customer", "C005"), ("block_card", "C005")]


def test_missing_session_id_groups_by_phone(store):
    mcp_tools.get_card_status("+919845001003")
    mcp_tools.get_recent_declines("9845001003")
    sessions = {r["session_id"] for r in store.query("SELECT session_id FROM tool_calls")}
    assert len(sessions) == 1 and next(iter(sessions)).startswith("auto-9845001003-")


def test_unknown_customer(store):
    assert "error" in mcp_tools.get_card_status("+910000000000", session_id="x")


def test_record_promise_from_the_ai(store):
    result = mcp_tools.record_promise("+919845001005", "Your new card will arrive in 2 days", "replacement",
                                      due="in 2 days", session_id="s-7")
    assert result["recorded"] and "policy_warning" not in result
    [p] = promises.list_promises(store, agent_type="ai")
    assert p["made_by"] == "CardCare" and p["status"] == "cant_keep"


def test_record_promise_live_guard(store, monkeypatch):
    monkeypatch.setenv("TRUSTLOOP_LIVE_GUARD", "1")
    result = mcp_tools.record_promise("+919845001005", "New card in 2 days", "replacement", due="2 days")
    assert "5–7 working days" in result["policy_warning"]


def test_record_promise_with_iso_due(store):
    result = mcp_tools.record_promise("+919845001009", "I'll call you back", "callback", due="2026-11-05T11:00:00")
    assert result["due_at"] == "2026-11-05T11:00:00+05:30"


def test_record_outcome_and_customer_promises(store):
    mcp_tools.record_outcome("card_declined", resolved=False, escalated=True, phone="+919845001002", session_id="s9")
    outcome = store.query_one("SELECT * FROM outcomes")
    assert outcome["customer_id"] == "C002" and outcome["escalated"] == 1
    mcp_tools.record_promise("+919845001002", "I'll call you back tomorrow", "callback", due="tomorrow")
    view = mcp_tools.get_customer_promises("+919845001002")
    assert view["open"] and "Open promise" in view["headline"]

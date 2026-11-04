import asyncio
import json
import re
from pathlib import Path

import pytest

from trustloop import mcp_tools, promises

ORCHESTRATOR = Path(__file__).resolve().parents[2] / "person_a" / "person_a" / "phase1" / "orchestrator" / "orchestrator.py"


def _call(name, args):
    server = mcp_tools.build_mcp()
    return json.loads(json.dumps(asyncio.run(server.call_tool(name, args)), default=str))


def test_server_lists_every_tool(store):
    tools = {t.name: t for t in asyncio.run(mcp_tools.build_mcp().list_tools())}
    assert set(tools) == set(mcp_tools.TOOLS)
    assert set(tools["verify_customer"].inputSchema["required"]) == {"phone", "last4", "dob"}
    assert set(tools["record_promise"].inputSchema["properties"]) == {"promise_text", "customer_id", "due_at", "session_id"}
    for t in tools.values():  # Talkdesk sends strings, so every parameter must be a string
        assert {p["type"] for p in t.inputSchema["properties"].values()} == {"string"}, t.name


@pytest.mark.skipif(not ORCHESTRATOR.exists(), reason="Person A's folder isn't next to this repo")
def test_contract_matches_person_a_orchestrator(store):
    """Every tool and parameter Person A's import file declares must exist on our server."""
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    block = source[source.index("TOOLS: dict"):source.index("\n}\n", source.index("TOOLS: dict"))]
    declared = {}
    for name, body in re.findall(r'\n    "(\w+)": \{(.*?)\n    \},', block, re.S):
        declared[name] = set(re.findall(r'\n\s+"(\w+)": \(', body))
    ours = {t.name: set(t.inputSchema["properties"]) for t in asyncio.run(mcp_tools.build_mcp().list_tools())}
    assert len(declared) == 8
    for name, params in declared.items():
        assert name in ours, name
        assert params <= ours[name], (name, params - ours[name])


def test_talkdesk_style_string_arguments(store):
    _call("set_international_usage", {"card_id": "CARD-9034", "enabled": "true", "session_id": "td-1"})
    _call("record_outcome", {"session_id": "td-2", "intent": "DECLINE", "resolved": "false", "escalated": "true"})
    card = store.query_one("SELECT international FROM cards WHERE customer_id = 'C1004'")
    outcome = store.query_one("SELECT resolved, escalated, intent FROM outcomes WHERE session_id = 'td-2'")
    assert card["international"] == 1 and (outcome["resolved"], outcome["escalated"]) == (0, 1)


def test_verify_returns_the_ids_other_tools_need(store):
    result = mcp_tools.verify_customer("+91 98450 33333", "5562", "2 Nov 1999", session_id="s1")
    assert (result["customer_id"], result["card_id"], result["first_name"]) == ("C1003", "CARD-5562", "Ananya")


def test_lookups_by_customer_id(store):
    status = mcp_tools.get_card_status("C1002", session_id="s2")
    assert status["replacement"]["status"] == "dispatched" and status["replacement"]["eta"] == "2026-11-10"
    declines = mcp_tools.get_recent_declines("C1001", session_id="s2")["declines"]
    assert declines[0]["reason"] == "Incorrect PIN entered"


def test_actions_by_card_id(store):
    assert mcp_tools.block_card("CARD-5562", "STOLEN", session_id="s3")["blocked"] is True
    result = mcp_tools.request_replacement("CARD-7310", "true", session_id="s3")
    assert "already dispatched" in result["warning"]


def test_calls_are_logged_per_session(store):
    mcp_tools.verify_customer("+919845033333", "5562", "1999-11-02", session_id="interaction-42")
    mcp_tools.block_card("CARD-5562", "STOLEN", session_id="interaction-42")
    rows = store.query("SELECT tool, customer_id FROM tool_calls WHERE session_id = 'interaction-42' ORDER BY id")
    assert [(r["tool"], r["customer_id"]) for r in rows] == [("verify_customer", "C1003"), ("block_card", "C1003")]


def test_missing_session_id_groups_by_customer(store):
    mcp_tools.verify_customer("+919845022222", "7310", "22 July 1991")
    mcp_tools.get_card_status("C1002")
    mcp_tools.request_replacement("CARD-7310", "true")
    sessions = {r["session_id"] for r in store.query("SELECT session_id FROM tool_calls")}
    assert len(sessions) == 1 and next(iter(sessions)).startswith("auto-C1002-")


def test_unknown_ids(store):
    assert "error" in mcp_tools.get_card_status("C9999", session_id="x")
    assert "error" in mcp_tools.block_card("CARD-0000", "LOST", session_id="x")


def test_record_promise_with_a_date_from_policy(store):
    result = mcp_tools.record_promise("Your new card will arrive in 7 working days.", customer_id="C1005",
                                      due_at="2026-11-13", session_id="interaction-7")
    assert result["recorded"] and result["due_at"] == "2026-11-13"
    [p] = promises.list_promises(store, agent_type="ai")
    assert p["kind"] == "replacement" and p["made_by"] == "CardCare"


def test_record_promise_that_breaks_policy(store):
    mcp_tools.record_promise("Your new card will arrive in about 2 days.", customer_id="C1005", due_at="in 2 days")
    [p] = promises.list_promises(store, agent_type="ai")
    assert p["status"] == "cant_keep" and "7 working days" in p["policy_violation"]


def test_record_promise_live_guard(store, monkeypatch):
    monkeypatch.setenv("TRUSTLOOP_LIVE_GUARD", "1")
    result = mcp_tools.record_promise("New card in 2 days", customer_id="C1005", due_at="2 days")
    assert "7 working days" in result["policy_warning"]


def test_customer_promises_view(store):
    mcp_tools.record_promise("I'll call you back tomorrow", customer_id="C1001", due_at="tomorrow")
    view = mcp_tools.get_customer_promises("C1001")
    assert view["open"] and "Open promise" in view["headline"]

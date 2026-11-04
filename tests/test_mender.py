import csv
import io
import json
from types import SimpleNamespace

import pytest

from trustloop import agentspec, evaluator, mcp_tools, mender, promises

SEEDED = {"F1": "policy_breach", "F2": "unnecessary_escalation", "F3": "missing_knowledge", "F4": "wrong_tool",
          "F5": "unkeepable_promise"}


@pytest.fixture
def captured(store):
    mender.ensure_v1(store)
    mender.simulate_traffic(store)
    mender.capture(store, None)
    return store


def _cases():
    return {c["id"]: c for c in evaluator.load_cases()}


def test_v1_spec_is_person_a_spec(store):
    v1 = mender.current_instructions(store)
    assert [a["name"] for a in v1["spec"]["agents"]][0] == "CardCare_Orchestrator_DFT"
    assert "Replacement cards usually arrive quickly, in about 2 days." in v1["prompt"]
    assert "SPECIALIST Card_Services_Agent_DFT" in v1["prompt"]


def test_replace_v1_with_the_imported_spec(captured):
    spec = mender.current_instructions(captured)["spec"]
    spec["agents"][0]["instruction"] += "\nExtra line from Person A."
    v1 = mender.replace_v1(captured, spec)
    assert v1["version"] == 1 and "Extra line from Person A." in v1["prompt"]
    assert mender.list_failures(captured) == []
    with pytest.raises(ValueError):
        mender.replace_v1(captured, {"agents": []})


def test_simulated_v1_traffic_shows_all_five_weaknesses(captured):
    assert {c["failure_type"] for c in mender.clusters(captured)} == set(SEEDED.values())


def test_capture_is_idempotent(captured):
    count = len(mender.list_failures(captured))
    assert mender.capture(captured, None)["new_failures"] == 0
    assert len(mender.list_failures(captured)) == count


def test_policy_breach_from_tool_order(store):
    mcp_tools.block_card("CARD-5562", "STOLEN", session_id="live-1")
    mcp_tools.verify_customer("+919845033333", "5562", "2 Nov 1999", session_id="live-1")
    mender.capture(store, None)
    [f] = mender.list_failures(store)
    assert f["failure_type"] == "policy_breach" and "before the customer was verified" in f["evidence"]


def test_verified_actions_are_not_failures(store):
    mcp_tools.verify_customer("+919845033333", "5562", "2 Nov 1999", session_id="live-2")
    mcp_tools.block_card("CARD-5562", "STOLEN", session_id="live-2")
    assert mender.capture(store, None)["new_failures"] == 0


def test_offline_transcript_diagnosis():
    text = ("Customer: My card was declined at Croma.\nCardCare: I'm transferring you to a human agent now.\n"
            "Customer: Can I use it in Dubai?\nCardCare: Sorry, the card is issued for domestic use.")
    assert [f["failure_type"] for f in mender._diagnose_offline(text)] == ["unnecessary_escalation", "missing_knowledge"]


def test_cases_are_person_a_cases(captured):
    cases = mender.build_cases(captured)
    assert [c["id"] for c in cases] == ["F1", "F2", "F3", "F4", "F5", "G1", "G2", "G3", "G4", "G5", "G6"]
    rows = list(csv.DictReader(io.StringIO(mender.cases_csv(cases))))
    assert "My details: Phone +91 98450 33333" in rows[0]["customer_message"]
    assert rows[0]["required_tools_in_order"] == "verify_customer -> block_card"


def test_v1_is_demo_ready(captured):
    """Person A's definition: on v1 every seeded case fails and every golden case passes."""
    results = evaluator.run_suite(mender.build_cases(captured), mender.current_instructions(captured)["prompt"], None)
    wrong = [(r["case_id"], r["reasons"]) for r in results if r["passed"] != (r["kind"] == "golden")]
    assert wrong == []


def test_person_a_example_replies_score_as_they_expect():
    """Our scorer agrees with Person A's: each case's example_v1 / example_good is scored the same way."""
    for case in evaluator.load_cases():
        for key, should_pass in (("example_good", True), ("example_v1", case["kind"] == "golden")):
            ex = case[key]
            trace = {"tools": [{"name": t} for t in ex["tools"]], "text": ex["reply"], "escalated": ex["escalated"]}
            assert evaluator.check_case(case, trace)["passed"] is should_pass, (case["id"], key)


def test_fix_patch_passes_the_gate_and_keeps_the_spec_importable(captured):
    mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    spec = agentspec.loads(patch["text"])
    prompt = agentspec.to_prompt(spec)
    for bad in ("about 2 days", "issued for domestic use", "Verification is not needed for blocking",
                "Declines need a specialist"):
        assert bad not in prompt, bad
    assert "7 working days" in prompt and "log it with record_promise" in prompt
    v1 = mender.current_instructions(captured)["spec"]
    assert [(a["name"], a["role"], a["tools"]) for a in spec["agents"]] == \
           [(a["name"], a["role"], a["tools"]) for a in v1["agents"]]
    assert {c["failure_type"] for c in patch["changes"]} == set(SEEDED.values())
    report = mender.evaluate_patch(captured, None, patch["id"])
    assert report["gate"]["decision"] == "PASS", report["gate"]
    assert (report["gate"]["score_before"], report["gate"]["score_after"]) == (55, 100)
    v2 = mender.approve(captured, patch["id"])
    assert v2["version"] == 2 and v2["spec"] == spec


def test_v2_follows_person_a_build_rules(captured):
    """Person A's orchestrator.py rejects a v2 that breaks these rules, so Mender never produces one."""
    patch = mender.propose_patch(captured, None)
    v1 = mender.current_instructions(captured)["spec"]
    v2 = agentspec.loads(patch["text"])
    assert agentspec.build_problems(v2, v1, set(mcp_tools.TOOLS)) == []
    assert [k for k in v2] == [k for k in v1]  # name, description, welcome_message, agents
    assert agentspec.all_tools(v2) == set(mcp_tools.TOOLS)  # all 9 tools still attached


def test_build_rules_catch_bad_specs(captured):
    v1 = mender.current_instructions(captured)["spec"]
    bad = json.loads(json.dumps(v1))
    bad["agents"][1]["name"] = "Identity_Agent"
    bad["agents"][2]["tools"] += ["get_recent_declines", "verify_customer"]
    bad["agents"][4]["tools"].remove("get_customer_promises")
    problems = agentspec.build_problems(bad, v1, set(mcp_tools.TOOLS))
    assert any("must end in _DFT" in p for p in problems)
    assert any("limit is 5" in p for p in problems)
    assert any("get_customer_promises" in p for p in problems)


def test_overeager_patch_is_blocked(captured):
    mender.build_cases(captured)
    patch = mender.propose_overeager_patch(captured)
    gate = mender.evaluate_patch(captured, None, patch["id"])["gate"]
    assert gate["decision"] == "BLOCK" and len(gate["regressions"]) >= 2
    with pytest.raises(PermissionError):
        mender.approve(captured, patch["id"])


def test_external_results_drive_the_gate(captured):
    cases = mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    before = {c["id"]: c["kind"] == "golden" for c in cases}
    after = {c["id"]: True for c in cases}
    assert mender.record_external_results(captured, patch["id"], before, after)["gate"]["decision"] == "PASS"
    after["G2"] = False
    assert mender.record_external_results(captured, patch["id"], before, after)["gate"]["decision"] == "BLOCK"


def test_llm_patch_is_merged_into_the_spec(captured):
    v1 = mender.current_instructions(captured)["spec"]
    sup = agentspec.supervisor(v1)
    payload = {"agents": [{"name": sup["name"], "instruction": sup["instruction"].replace("about 2 days", "7 working days"),
                           "routing_condition": sup["routing_condition"]}],
               "changes": [{"failure_type": "unkeepable_promise", "change": "7 working days", "rationale": "policy"}]}
    llm = _fake_llm(payload)
    patch = mender.propose_patch(captured, llm)
    assert patch["source"] == "gemini" and "7 working days" in agentspec.to_prompt(agentspec.loads(patch["text"]))


def test_patch_with_unknown_tool_falls_back_to_rules(captured):
    sup = agentspec.supervisor(mender.current_instructions(captured)["spec"])
    payload = {"agents": [{"name": sup["name"], "instruction": "Use `refund_everything` for all problems.",
                           "routing_condition": ""}], "changes": []}
    assert mender.propose_patch(captured, _fake_llm(payload))["source"] == "rules"


def _fake_llm(payload):
    return SimpleNamespace(name="gemini", json=lambda **kw: payload)


class NeverCall:
    name = "gemini"

    def run_agent(self, **kwargs):
        raise AssertionError("evaluation must not call the model unless asked")


def test_evaluation_uses_no_model_calls_by_default(captured):
    mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    report = mender.evaluate_patch(captured, NeverCall(), patch["id"])
    assert report["engine"] == "simulator" and report["gate"]["decision"] == "PASS"


def test_import_person_a_export(store):
    """The JSON that Person A's `cases.py export` writes becomes CardCare traffic Mender can learn from."""
    cases = _cases()
    sessions = []
    for cid, tools, escalated, reply in (
        ("F1", ["block_card"], False, cases["F1"]["example_v1"]["reply"]),
        ("F2", [], True, cases["F2"]["example_v1"]["reply"]),
        ("F3", [], False, cases["F3"]["example_v1"]["reply"]),
        ("F4", ["verify_customer", "request_replacement"], False, cases["F4"]["example_v1"]["reply"]),
        ("F5", ["verify_customer", "request_replacement"], False, cases["F5"]["example_v1"]["reply"]),
    ):
        sessions.append({"session_id": f"{cid}-v1-1", "case_id": cid, "customer_id": cases[cid]["customer_id"],
                         "turns": [{"role": "customer", "text": cases[cid]["say"][0]}, {"role": "agent", "text": reply}],
                         "tool_calls": tools, "escalated": escalated})
    assert mender.import_runs(store, {"version": "v1", "sessions": sessions}) == {"imported": 5, "skipped_not_as_planned": 0}
    mender.capture(store, None)
    assert {c["failure_type"] for c in mender.clusters(store)} == set(SEEDED.values())
    [p] = promises.list_promises(store, agent_type="ai")
    assert p["session_id"] == "F5-v1-1" and p["status"] == "cant_keep"


def test_customer_message_for_scripted_chats():
    cases = _cases()
    assert evaluator.customer_message(cases["G5"]).endswith("My details: phone +91 98450 66666, last 4 digits 1111, "
                                                            "date of birth 18 Dec 1985")
    assert "I'm not Rahul and I don't have his card details." in evaluator.customer_message(cases["G6"])
    assert "Please use my registered address." in evaluator.customer_message(cases["F5"])
    assert evaluator.customer_message(cases["G2"]) == cases["G2"]["say"][0]


def test_import_skips_chats_that_did_not_fail_as_planned(store):
    session = {"session_id": "F3-v1-2", "case_id": "F3", "customer_id": "C1004", "failed_as_planned": False,
               "turns": [{"role": "customer", "text": "Can I use my card in Dubai?"},
                         {"role": "agent", "text": "Yes, once international usage is enabled."}],
               "tool_calls": [], "escalated": False}
    assert mender.import_runs(store, {"sessions": [session]}) == {"imported": 0, "skipped_not_as_planned": 1}
    assert store.query("SELECT * FROM transcripts") == []


def test_reset_bank_restores_cards_and_keeps_logs(store):
    from trustloop import bank
    mcp_tools.block_card("CARD-5562", "STOLEN", session_id="F1-run")
    mcp_tools.request_replacement("CARD-7310", "true", session_id="F4-run")
    bank.reset_bank(store)
    assert store.query_one("SELECT status FROM cards WHERE card_id = 'CARD-5562'")["status"] == "active"
    rahul = store.query_one("SELECT replacement_card_id, replacement_status FROM cards WHERE customer_id = 'C1002'")
    assert (rahul["replacement_card_id"], rahul["replacement_status"]) == ("CARD-7311", "dispatched")
    assert len(store.query("SELECT * FROM tool_calls")) == 2  # the logs survive

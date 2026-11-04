import csv
import io
import json
from types import SimpleNamespace

import pytest

from trustloop import evaluator, mcp_tools, mender, promises
from pathlib import Path

AI = (Path(__file__).resolve().parent.parent / "demo" / "transcripts" / "ai_meera_replacement.txt").read_text("utf-8")


@pytest.fixture
def captured(store):
    mender.simulate_traffic(store)
    mender.capture(store, None)
    return store


def test_simulated_v1_traffic_shows_all_five_weaknesses(captured):
    types = {c["failure_type"] for c in mender.clusters(captured)}
    assert types == {"policy_breach", "unnecessary_escalation", "missing_knowledge", "wrong_tool", "unkeepable_promise"}


def test_capture_is_idempotent(captured):
    count = len(mender.list_failures(captured))
    assert mender.capture(captured, None)["new_failures"] == 0
    assert len(mender.list_failures(captured)) == count


def test_policy_breach_from_tool_order(store):
    mcp_tools.block_card("+919845001005", "stolen", session_id="live-1")
    mcp_tools.verify_customer("+919845001005", "6604", "1995-05-09", session_id="live-1")
    mender.capture(store, None)
    [f] = mender.list_failures(store)
    assert f["failure_type"] == "policy_breach" and "before the customer was verified" in f["evidence"]


def test_verified_actions_are_not_failures(store):
    mcp_tools.verify_customer("+919845001005", "6604", "1995-05-09", session_id="live-2")
    mcp_tools.block_card("+919845001005", "stolen", session_id="live-2")
    assert mender.capture(store, None)["new_failures"] == 0


def test_ai_transcript_promise_becomes_a_failure(store):
    promises.ingest_transcript(store, None, text=AI, agent_name="CardCare", agent_type="ai", phone="+919845001005")
    mender.capture(store, None)
    assert [f["failure_type"] for f in mender.list_failures(store)] == ["unkeepable_promise"]


def test_offline_transcript_diagnosis():
    text = ("Customer: My card was declined at the mall.\nCardCare: I'll connect you to a human agent.\n"
            "Customer: Can I use it abroad?\nCardCare: Sorry, international usage isn't possible over chat.")
    types = [f["failure_type"] for f in mender._diagnose_offline(text)]
    assert types == ["unnecessary_escalation", "missing_knowledge"]


def test_cases_cover_clusters_and_golden(captured):
    cases = mender.build_cases(captured)
    assert sum(c["kind"] == "failure" for c in cases) == 10
    assert sum(c["kind"] == "golden" for c in cases) == 6
    rows = list(csv.DictReader(io.StringIO(mender.cases_csv(cases))))
    assert rows[0]["customer_message"].startswith("Hi, my phone number is")


def test_v1_fails_failure_cases_and_passes_golden(captured):
    cases = mender.build_cases(captured)
    v1 = mender.current_instructions(captured)["text"]
    results = {r["case_id"]: r for r in evaluator.run_suite(cases, v1, None)}
    assert all(not r["passed"] for r in results.values() if r["kind"] == "failure"), \
        [r for r in results.values() if r["kind"] == "failure" and r["passed"]]
    assert all(r["passed"] for r in results.values() if r["kind"] == "golden"), \
        [(r["case_id"], r["reasons"]) for r in results.values() if r["kind"] == "golden" and not r["passed"]]


def test_fix_patch_passes_the_gate_and_can_be_approved(captured):
    mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    assert "arrive in 2 days" not in patch["text"]
    assert "-New cards arrive in 2 days." in patch["diff"].replace("- New", "-New")
    report = mender.evaluate_patch(captured, None, patch["id"])
    assert report["gate"]["decision"] == "PASS", report["gate"]
    assert report["gate"]["score_after"] == 100 and len(report["gate"]["fixed"]) == 10
    version = mender.approve(captured, patch["id"])
    assert version["version"] == 2 and version["text"] == patch["text"]


def test_overeager_patch_is_blocked(captured):
    mender.build_cases(captured)
    patch = mender.propose_overeager_patch(captured)
    report = mender.evaluate_patch(captured, None, patch["id"])
    gate = report["gate"]
    assert gate["decision"] == "BLOCK"
    assert set(gate["regressions"]) >= {"G1-card-status", "G4-damaged-card", "G6-is-card-blocked"}
    with pytest.raises(PermissionError):
        mender.approve(captured, patch["id"])


def test_external_results_drive_the_gate(captured):
    cases = mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    before = {c["id"]: c["kind"] == "golden" for c in cases}
    after = {c["id"]: True for c in cases}
    assert mender.record_external_results(captured, patch["id"], before, after)["gate"]["decision"] == "PASS"
    after["G2-greeting"] = False
    assert mender.record_external_results(captured, patch["id"], before, after)["gate"]["decision"] == "BLOCK"


def test_patch_with_unknown_tool_falls_back_to_rules(captured):
    payload = {"instructions": "Use `refund_everything` for all problems.", "changes": []}
    messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn", stop_details=None))
    from trustloop.llm import ClaudeLLM
    llm = ClaudeLLM(SimpleNamespace(model="m", use_fallbacks=False),
                    client=SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)))
    patch = mender.propose_patch(captured, llm)
    assert patch["source"] == "rules"


class NeverCall:
    name = "gemini"

    def run_agent(self, **kwargs):
        raise AssertionError("evaluation must not call the model unless asked")


def test_evaluation_uses_no_model_calls_by_default(captured):
    mender.build_cases(captured)
    patch = mender.propose_patch(captured, None)
    report = mender.evaluate_patch(captured, NeverCall(), patch["id"])
    assert report["engine"] == "simulator" and report["gate"]["decision"] == "PASS"


def test_check_case_reasons():
    case = {"expect": {"required_tools": ["verify_customer"], "before": {"block_card": "verify_customer"},
                       "must_not_mention": ["2 days"], "should_escalate": False}}
    trace = {"tools": [{"name": "block_card"}], "text": "New card in 2 days. I'll connect you to a human agent.",
             "escalated": True}
    reasons = evaluator.check_case(case, trace)["reasons"]
    assert reasons == ["never called verify_customer", "called block_card before verify_customer",
                       "said \"2 days\"", "handed over to a human"]


def test_dash_variants_match():
    case = {"expect": {"must_mention_any": ["5-7 working days"]}}
    assert evaluator.check_case(case, {"tools": [], "text": "It takes 5–7 working days.", "escalated": False})["passed"]

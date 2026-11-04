import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from trustloop import clock, promises
from trustloop.config import load_settings
from trustloop.llm import ClaudeLLM, LLMError

TRANSCRIPTS = Path(__file__).resolve().parent.parent / "demo" / "transcripts"
HUMAN = (TRANSCRIPTS / "human_fatima_dispute.txt").read_text(encoding="utf-8")
AI = (TRANSCRIPTS / "ai_meera_replacement.txt").read_text(encoding="utf-8")


def _settings():
    return load_settings()


def test_parse_transcript_roles():
    turns = promises.parse_transcript(AI, agent_name="CardCare")
    assert [t["role"] for t in turns] == ["customer", "agent", "customer", "agent"]


def test_parse_transcript_with_timestamps():
    text = "[10:02 AM] Agent: I'll call you back tomorrow.\n10:03 Customer: Thanks\nAgent Ravi: Bye"
    roles = [(t["speaker"], t["role"]) for t in promises.parse_transcript(text)]
    assert roles == [("Agent", "agent"), ("Customer", "customer"), ("Agent Ravi", "agent")]


def test_offline_extraction_splits_two_promises():
    found = promises.extract_offline(HUMAN)
    assert [p["kind"] for p in found] == ["callback", "document"]
    assert found[0]["due_phrase"] == "thursday"
    assert found[1]["due_phrase"] == "today"


def test_offline_extraction_ignores_questions_and_customer_lines():
    text = "Customer: I'll wait for the call.\nAgent: Shall I call you back tomorrow?\nAgent: Thanks for waiting."
    assert promises.extract_offline(text) == []


def test_ingest_human_transcript(store):
    result = promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    assert result["customer"]["name"] == "Fatima Sheikh"
    kinds = {p["kind"]: p for p in result["promises"]}
    assert set(kinds) == {"callback", "document"}
    assert kinds["callback"]["policy_violation"] == ""
    assert kinds["callback"]["status"] in ("open", "due_soon")


def test_ai_promise_that_breaks_policy(store):
    result = promises.ingest_transcript(store, None, text=AI, agent_name="CardCare", agent_type="ai",
                                        phone="+919845001005")
    [promise] = result["promises"]
    assert promise["kind"] == "replacement"
    assert "5–7 working days" in promise["policy_violation"]
    assert promise["status"] == "cant_keep"


def test_promise_breaks_when_the_clock_passes_due(store):
    promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    clock.advance(store, 24 * 8)
    statuses = {p["kind"]: p["status"] for p in promises.list_promises(store)}
    assert statuses == {"callback": "broken", "document": "broken"}
    card = promises.card(store, _settings(), phone="9845001009")
    assert "It hasn't happened" in card["headline"]
    assert len(card["broken"]) == 2


def test_bank_event_keeps_a_promise(store):
    result = promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    clock.advance(store, 1)
    from trustloop import bank
    bank.simulate(store, "C009", "callback_completed")
    promises.sweep(store, _settings())
    callback = next(p for p in result["promises"] if p["kind"] == "callback")
    assert promises.get_promise(store, callback["id"])["status"] == "kept"


def test_sweep_queues_each_message_once(store):
    promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    clock.advance(store, 24 * 8)
    promises.sweep(store, _settings())
    promises.sweep(store, _settings())
    purposes = sorted(m["purpose"] for m in store.query("SELECT purpose FROM messages"))
    assert purposes == ["apology", "apology", "broken_alert", "broken_alert"]


def test_supervisor_alert_for_cant_keep(store):
    promises.ingest_transcript(store, None, text=AI, agent_name="CardCare", agent_type="ai", phone="+919845001005")
    promises.sweep(store, _settings())
    alert = store.query_one("SELECT * FROM messages WHERE purpose = 'cant_keep_alert'")
    assert alert["audience"] == "supervisor"


def test_mark_kept_and_dismiss(store):
    result = promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    first, second = result["promises"]
    assert promises.mark_kept(store, first["id"], "Emailed")["status"] == "kept"
    assert promises.dismiss(store, second["id"])["status"] == "dismissed"


def test_dashboard_counts(store):
    promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    promises.ingest_transcript(store, None, text=AI, agent_name="CardCare", agent_type="ai", phone="+919845001005")
    board = promises.dashboard(store, _settings())
    assert board["total"] == 3
    assert board["by_agent"]["CardCare"]["cant_keep"] == 1


def test_repeat_contact_after_broken_promise(store):
    promises.ingest_transcript(store, None, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    clock.advance(store, 24 * 8)
    promises.ingest_transcript(store, None, text="Customer: Nobody called me back!\nAgent: I'm so sorry.",
                               agent_name="Asha", phone="+919845001009")
    assert promises.dashboard(store, _settings())["repeat_contacts_after_broken"] == 2


def test_no_date_promise_gets_default(store):
    text = "Agent: I'll look into the charge and get back to you."
    [p] = promises.ingest_transcript(store, None, text=text, agent_name="Ravi", phone="+919845001001")["promises"]
    assert p["no_date"] is True and p["kind"] == "callback"


# --- Claude path, with a fake client standing in for the API ---------------------------------

class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="text", text=json.dumps(self.payload))
        return SimpleNamespace(content=[block], stop_reason=self.stop_reason, stop_details=None)


def _fake_llm(payload, stop_reason="end_turn"):
    messages = FakeMessages(payload, stop_reason)
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    settings = SimpleNamespace(model="claude-opus-5-5", use_fallbacks=True)
    return ClaudeLLM(settings, client=client), messages


def test_claude_extraction_request_and_parsing(store):
    payload = {"promises": [{"quote": "I'll call you back on Thursday", "kind": "callback",
                             "description": "Call Fatima back about the Uber dispute",
                             "due_phrase": "on Thursday", "due_at": ""}]}
    llm, fake = _fake_llm(payload)
    result = promises.ingest_transcript(store, llm, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    assert result["engine"] == "claude"
    assert result["promises"][0]["description"] == "Call Fatima back about the Uber dispute"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Transcript:" in call["messages"][0]["content"]


def test_claude_refusal_falls_back_to_offline(store):
    llm, _ = _fake_llm({"promises": []}, stop_reason="refusal")
    result = promises.ingest_transcript(store, llm, text=HUMAN, agent_name="Ravi", phone="+919845001009")
    assert result["engine"] == "offline"
    assert len(result["promises"]) == 2


def test_claude_json_raises_on_refusal():
    llm, _ = _fake_llm({}, stop_reason="refusal")
    try:
        llm.json(system="s", prompt="p", schema={"type": "object"})
    except LLMError:
        return
    raise AssertionError("expected LLMError")


def test_due_soon_window(store):
    created = clock.now(store)
    row = {"dismissed": 0, "kept_at": None, "policy_violation": "",
           "due_at": clock.iso(created + timedelta(hours=5))}
    assert promises.status_of(row, created) == "due_soon"
    row["due_at"] = clock.iso(created + timedelta(hours=30))
    assert promises.status_of(row, created) == "open"

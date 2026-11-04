import json
from types import SimpleNamespace

import pytest
from google.genai import types

from trustloop import evaluator, mender, promises
from trustloop.config import load_settings
from trustloop.llm import GeminiLLM, LLMError, get_llm


def test_provider_selection(monkeypatch):
    for var in ("TRUSTLOOP_LLM", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert load_settings().llm_mode == "offline"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert load_settings().llm_mode == "claude"
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    settings = load_settings()
    assert settings.llm_mode == "gemini" and settings.gemini_api_key == "g"
    monkeypatch.setenv("TRUSTLOOP_LLM", "offline")
    assert get_llm(load_settings()) is None


class FakeModels:
    def __init__(self, responses=(), names=(), probe_fail=()):
        self.responses, self.names, self.calls = list(responses), names, []
        self.probe_fail, self.probes = set(probe_fail), []

    def generate_content(self, *, model, contents, config):
        if contents == "Reply with: ok":  # the startup probe
            self.probes.append(model)
            if model in self.probe_fail:
                raise ApiError(503)
            return _text_response("ok")
        self.calls.append({"model": model, "contents": list(contents) if isinstance(contents, list) else contents,
                           "config": config})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def list(self):
        return [SimpleNamespace(name=f"models/{n}", supported_actions=["generateContent"]) for n in self.names]


def _gemini(responses=(), names=(), model="", probe_fail=()):
    fake = FakeModels(responses, names, probe_fail)
    settings = SimpleNamespace(model=model, gemini_api_key="g")
    return GeminiLLM(settings, client=SimpleNamespace(models=fake)), fake


def _text_response(text, finish="STOP"):
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content, finish_reason=finish)],
                           function_calls=None, text=text)


def _call_response(name, args):
    call = types.FunctionCall(name=name, args=args)
    content = types.Content(role="model", parts=[types.Part(function_call=call)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content, finish_reason="STOP")],
                           function_calls=[call], text=None)


def test_model_auto_pick_prefers_newest_stable_flash():
    names = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-9.0-flash-preview",
             "gemini-2.5-pro", "text-embedding-004", "gemini-2.5-flash-image"]
    llm, _ = _gemini(names=names)
    assert llm.model == "gemini-2.5-flash"


def test_model_auto_pick_uses_preview_when_nothing_stable():
    llm, _ = _gemini(names=["gemini-3.0-flash-preview", "gemini-2.5-pro"])
    assert llm.model == "gemini-3.0-flash-preview"


def test_explicit_model_skips_listing():
    llm, fake = _gemini(model="gemini-custom")
    assert llm.model == "gemini-custom"


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def test_failover_to_next_model_and_stick_with_it():
    names = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]
    llm, fake = _gemini([ApiError(503), _text_response('{"ok": true}'), _text_response('{"ok": true}')], names=names)
    assert llm.json(system="s", prompt="p", schema={"type": "object"}) == {"ok": True}
    assert [c["model"] for c in fake.calls] == ["gemini-3.8-flash", "gemini-3.5-flash"]
    llm.json(system="s", prompt="p", schema={"type": "object"})
    assert fake.calls[-1]["model"] == "gemini-3.5-flash"  # the overloaded model isn't retried first


def test_retired_model_404_fails_over():
    llm, fake = _gemini([ApiError(404), _text_response('{"ok": true}')], names=["gemini-2.5-flash", "gemini-3.5-flash"])
    assert llm.json(system="s", prompt="p", schema={"type": "object"}) == {"ok": True}


def test_bad_request_is_not_retried():
    llm, fake = _gemini([ApiError(400)], names=["gemini-3.8-flash", "gemini-3.5-flash"])
    with pytest.raises(ApiError):
        llm.json(system="s", prompt="p", schema={"type": "object"})
    assert len(fake.calls) == 1


def test_all_models_down_raises_llm_error(store):
    llm, _ = _gemini([ApiError(503), ApiError(503)], names=["gemini-3.8-flash", "gemini-3.5-flash"])
    with pytest.raises(LLMError):
        llm.json(system="s", prompt="p", schema={"type": "object"})
    llm, _ = _gemini([ApiError(503), ApiError(503)], names=["gemini-3.8-flash", "gemini-3.5-flash"])
    result = promises.ingest_transcript(store, llm, text="Agent: I'll call you back tomorrow.", agent_name="Ravi",
                                        phone="+919845001009")
    assert result["engine"] == "offline" and len(result["promises"]) == 1


def test_probe_skips_a_model_that_is_down():
    llm, fake = _gemini(names=["gemini-3.8-flash", "gemini-3.5-flash"], probe_fail={"gemini-3.8-flash"})
    assert llm.model == "gemini-3.5-flash"
    assert fake.probes == ["gemini-3.8-flash", "gemini-3.5-flash"]
    assert llm.candidates[-1] == "gemini-3.8-flash"


def test_preference_list_comes_first():
    names = ["gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"]
    llm, fake = _gemini(names=names, model="gemini-3-flash-preview, gemini-3.5-flash-lite")
    assert llm.candidates == ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
    assert fake.probes == ["gemini-3-flash-preview"]


def test_lite_models_rank_last():
    llm, _ = _gemini(names=["gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-3.5-flash", "gemini-flash-latest"])
    assert llm.candidates == ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest", "gemini-3.1-flash-lite"]


def test_json_uses_structured_output():
    llm, fake = _gemini([_text_response('{"promises": []}')], model="gemini-x")
    schema = {"type": "object", "properties": {"promises": {"type": "array"}}}
    assert llm.json(system="sys", prompt="p", schema=schema) == {"promises": []}
    config = fake.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema and config.system_instruction == "sys"


def test_safety_block_raises():
    llm, _ = _gemini([_text_response("", finish="SAFETY")], model="gemini-x")
    with pytest.raises(LLMError):
        llm.json(system="s", prompt="p", schema={"type": "object"})


def test_invalid_json_raises():
    llm, _ = _gemini([_text_response("not json")], model="gemini-x")
    with pytest.raises(LLMError):
        llm.json(system="s", prompt="p", schema={"type": "object"})


def test_agent_loop_runs_tools_and_returns_text():
    llm, fake = _gemini([
        _call_response("get_card_status", {"phone": "+919845001003"}),
        _text_response("Your card is on its way."),
    ], model="gemini-x")
    seen = []
    tools = [{"name": "get_card_status", "description": "Status", "input_schema":
              {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}}]
    calls, text = llm.run_agent(system="You are CardCare", user_message="Where is my card?", tools=tools,
                                run_tool=lambda name, args: seen.append((name, args)) or {"status": "in_transit"})
    assert seen == [("get_card_status", {"phone": "+919845001003"})]
    assert calls[0]["result"] == {"status": "in_transit"} and text == "Your card is on its way."
    second = fake.calls[1]["contents"]
    assert second[-1].parts[0].function_response.name == "get_card_status"
    declaration = fake.calls[0]["config"].tools[0].function_declarations[0]
    assert declaration.parameters_json_schema["required"] == ["phone"]


def test_gemini_promise_extraction(store):
    payload = {"promises": [{"quote": "I'll call you back on Thursday", "kind": "callback",
                             "description": "Call Fatima back", "due_phrase": "on Thursday", "due_at": ""}]}
    llm, _ = _gemini([_text_response(json.dumps(payload))], model="gemini-x")
    result = promises.ingest_transcript(store, llm, text="Agent: I'll call you back on Thursday.",
                                        agent_name="Ravi", phone="+919845001009")
    assert result["engine"] == "gemini" and result["promises"][0]["description"] == "Call Fatima back"


class ScriptedAgent:
    """A stand-in LLM whose agent always verifies, then checks the card status."""
    name = "gemini"

    def run_agent(self, *, system, user_message, tools, run_tool, max_turns):
        calls = []
        for name, args in (("verify_customer", {"phone": "+919845001010", "last4": "3390", "dob": "1986-02-28"}),
                           ("get_card_status", {"phone": "+919845001010"})):
            calls.append({"name": name, "args": args, "result": run_tool(name, args)})
        return calls, "Your card is active."


def test_evaluator_runs_any_provider(store):
    [golden] = [c for c in mender.golden_cases() if c["id"] == "G1-card-status"]
    result = evaluator.run_case(golden, "instructions", ScriptedAgent())
    assert result["engine"] == "gemini" and result["passed"], result
    assert result["tools"] == ["verify_customer", "get_card_status"]

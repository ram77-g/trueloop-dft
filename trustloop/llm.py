"""LLM access through one small interface, backed by Claude or Gemini.

Every caller also has an offline path, used when no key is set or a call fails."""

import json
import logging
import re
from typing import Callable

from .config import Settings

log = logging.getLogger("trustloop.llm")

ToolRunner = Callable[[str, dict], dict]


class LLMError(RuntimeError):
    pass


class ClaudeLLM:
    name = "claude"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, client=None):
        import anthropic

        self._anthropic = anthropic
        self.model = settings.model or "claude-opus-5-5"
        self.use_fallbacks = settings.use_fallbacks
        self.client = client or anthropic.Anthropic(max_retries=2, timeout=120.0)

    def _create(self, **kwargs):
        if self.use_fallbacks:
            try:
                return self.client.beta.messages.create(betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs)
            except self._anthropic.BadRequestError as exc:
                if "fallback" not in str(exc).lower():
                    raise
                log.warning("Server-side fallbacks rejected (%s); continuing without them.", exc)
                self.use_fallbacks = False
        return self.client.messages.create(**kwargs)

    @staticmethod
    def _check(response) -> None:
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMError(f"Claude declined the request: {getattr(details, 'category', None)}")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude ran out of output tokens")

    def json(self, *, system: str, prompt: str, schema: dict, effort: str = "low", max_tokens: int = 8000) -> dict:
        response = self._create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        self._check(response)
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude returned invalid JSON: {exc}") from exc

    def run_agent(self, *, system: str, user_message: str, tools: list[dict], run_tool: ToolRunner,
                  max_turns: int = 8) -> tuple[list[dict], str]:
        """Play the agent: loop until it stops calling tools. Returns (tool calls, everything it said)."""
        messages = [{"role": "user", "content": user_message}]
        calls, texts = [], []
        for _ in range(max_turns):
            response = self._create(model=self.model, max_tokens=4000, system=system, messages=messages,
                                    tools=tools, output_config={"effort": "low"})
            if response.stop_reason == "refusal":
                self._check(response)
            texts += [b.text for b in response.content if b.type == "text"]
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                args = dict(block.input)
                output = run_tool(block.name, args)
                calls.append({"name": block.name, "args": args, "result": output})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, ensure_ascii=False, default=str),
                                "is_error": "error" in output})
            messages.append({"role": "user", "content": results})
        return calls, "\n".join(texts)


class GeminiLLM:
    name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"
    _SKIP = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "8b", "vision", "robotics")

    def __init__(self, settings: Settings, client=None):
        from google import genai
        from google.genai import types

        self.types = types
        self.client = client or genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.model

    @property
    def model(self) -> str:
        if not self._model:
            self._model = self._pick_model()
        return self._model

    def _pick_model(self) -> str:
        """Choose the newest Flash model this key can use, preferring stable releases over previews."""
        try:
            names = [m.name.removeprefix("models/") for m in self.client.models.list()
                     if "generateContent" in (m.supported_actions or [])]
        except Exception as exc:  # no network or a bad key: fall back to a known name and let calls report it
            log.warning("Could not list Gemini models (%s); using %s", exc, self.DEFAULT_MODEL)
            return self.DEFAULT_MODEL
        flash = [n for n in names if "flash" in n and not any(s in n for s in self._SKIP)]

        def rank(name: str):
            version = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            return ("preview" not in name, float(version.group(1)) if version else 0.0, -len(name))

        chosen = max(flash, key=rank) if flash else self.DEFAULT_MODEL
        log.info("Using Gemini model %s", chosen)
        return chosen

    def _generate(self, contents, config):
        response = self.client.models.generate_content(model=self.model, contents=contents, config=config)
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise LLMError(f"Gemini returned no answer: {getattr(response, 'prompt_feedback', None)}")
        reason = str(getattr(candidates[0], "finish_reason", "") or "")
        if any(r in reason for r in ("SAFETY", "PROHIBITED", "BLOCKLIST", "RECITATION")):
            raise LLMError(f"Gemini declined the request: {reason}")
        if "MAX_TOKENS" in reason:
            raise LLMError("Gemini ran out of output tokens")
        return response

    def json(self, *, system: str, prompt: str, schema: dict, effort: str = "low", max_tokens: int = 8000) -> dict:
        config = self.types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            max_output_tokens=max(max_tokens, 8000),
        )
        response = self._generate(prompt, config)
        try:
            return json.loads(response.text or "")
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini returned invalid JSON: {exc}") from exc

    def run_agent(self, *, system: str, user_message: str, tools: list[dict], run_tool: ToolRunner,
                  max_turns: int = 8) -> tuple[list[dict], str]:
        t = self.types
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=[t.Tool(function_declarations=[
                t.FunctionDeclaration(name=x["name"], description=x["description"],
                                      parameters_json_schema=x["input_schema"]) for x in tools])],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [t.Content(role="user", parts=[t.Part.from_text(text=user_message)])]
        calls, texts = [], []
        for _ in range(max_turns):
            response = self._generate(contents, config)
            content = response.candidates[0].content
            parts = (content.parts if content else None) or []
            texts += [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
            function_calls = response.function_calls or []
            if not function_calls:
                break
            contents.append(content)  # kept whole so Gemini's thought signatures survive the round trip
            replies = []
            for fc in function_calls:
                args = dict(fc.args or {})
                output = run_tool(fc.name, args)
                calls.append({"name": fc.name, "args": args, "result": output})
                replies.append(t.Part.from_function_response(name=fc.name, response={"result": output}))
            contents.append(t.Content(role="user", parts=replies))
        return calls, "\n".join(texts)


LLM = ClaudeLLM | GeminiLLM


def get_llm(settings: Settings) -> LLM | None:
    if settings.llm_mode == "claude":
        return ClaudeLLM(settings)
    if settings.llm_mode == "gemini":
        return GeminiLLM(settings)
    return None

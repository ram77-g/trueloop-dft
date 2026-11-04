"""Runtime settings, all read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AGENT_DIR = ROOT / "agent"
PROVIDERS = ("claude", "gemini", "offline")


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str
    api_key: str
    model: str  # "" = the provider's default (Gemini picks its newest Flash model)
    llm_mode: str  # "claude", "gemini" or "offline"
    use_fallbacks: bool
    webhook_url: str
    live_guard: bool
    gemini_api_key: str = ""

    @property
    def llm_enabled(self) -> bool:
        return self.llm_mode != "offline"


def load_settings() -> Settings:
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    has_claude = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    default = "gemini" if gemini_key else ("claude" if has_claude else "offline")
    mode = os.environ.get("TRUSTLOOP_LLM", default).strip().lower()
    if mode not in PROVIDERS:
        mode = default
    return Settings(
        db_path=os.environ.get("TRUSTLOOP_DB", str(ROOT / "trustloop.db")),
        api_key=os.environ.get("TRUSTLOOP_API_KEY", ""),
        model=os.environ.get("TRUSTLOOP_MODEL", ""),
        llm_mode=mode,
        use_fallbacks=_flag("TRUSTLOOP_FALLBACKS", True),
        webhook_url=os.environ.get("TRUSTLOOP_WEBHOOK_URL", ""),
        live_guard=_flag("TRUSTLOOP_LIVE_GUARD", False),
        gemini_api_key=gemini_key,
    )

"""Bank policy: the text the LLM reads and the rules Promise Keeper checks promises against.

The rules mirror data/policy.md (Tarang Bank's policy, shared with Person A)."""

import re
from datetime import datetime
from functools import lru_cache

from .config import DATA_DIR
from .dates import add_working_days

PROMISE_KINDS = ["callback", "refund", "document", "replacement", "international", "follow_up"]
# The fastest the bank can honestly deliver each kind of promise.
MIN_WORKING_DAYS = {"replacement": 7, "refund": 5}
WHAT = {"replacement": "Replacement cards take 7 working days", "refund": "Failed-transaction reversals take 5 working days"}
TIME_SLOT = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\bnoon\b|\bat \d{1,2}\b", re.I)


@lru_cache(maxsize=1)
def policy_text() -> str:
    return (DATA_DIR / "policy.md").read_text(encoding="utf-8")


def check_promise(kind: str, created_at: datetime, due_at: datetime, text: str = "") -> str:
    """Return why the promise breaks policy, or "" if it can be kept."""
    if kind in MIN_WORKING_DAYS:
        earliest = add_working_days(created_at, MIN_WORKING_DAYS[kind])
        if due_at.date() < earliest.date():
            return (f"{WHAT[kind]}, so the earliest honest date is {earliest:%a %d %b}. "
                    f"The promise says {due_at:%a %d %b}.")
    if kind == "callback" and TIME_SLOT.search(text or ""):
        return "Callbacks happen within 1 working day, but no specific time slot is guaranteed."
    return ""

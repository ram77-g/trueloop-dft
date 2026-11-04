"""Bank policy: the text the LLM reads and the rules Promise Keeper checks promises against."""

from datetime import datetime
from functools import lru_cache

from .config import DATA_DIR
from .dates import add_working_days

PROMISE_KINDS = ["callback", "refund", "document", "replacement", "international", "follow_up"]
MIN_WORKING_DAYS = {"replacement": 5, "refund": 5}
CALLBACK_HOURS = (9, 18)


@lru_cache(maxsize=1)
def policy_text() -> str:
    return (DATA_DIR / "policy.md").read_text(encoding="utf-8")


def check_promise(kind: str, created_at: datetime, due_at: datetime) -> str:
    """Return why the promise breaks policy, or "" if it can be kept."""
    if kind in MIN_WORKING_DAYS:
        days = MIN_WORKING_DAYS[kind]
        earliest = add_working_days(created_at, days)
        if due_at.date() < earliest.date():
            what = "Replacement cards take" if kind == "replacement" else "Refunds take"
            return (f"{what} 5–7 working days, so the earliest honest date is "
                    f"{earliest:%a %d %b}. The promise says {due_at:%a %d %b}.")
    if kind == "callback":
        if due_at.weekday() == 6:
            return "Callbacks run Monday to Saturday. This promise falls on a Sunday."
        start, end = CALLBACK_HOURS
        minutes = due_at.hour * 60 + due_at.minute
        if not start * 60 <= minutes <= end * 60:
            return f"Callbacks run {start}:00–{end}:00 IST. This promise is for {due_at:%H:%M}."
    return ""

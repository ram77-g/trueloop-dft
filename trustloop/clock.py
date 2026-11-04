"""The demo clock: real time in IST plus an offset the console can move forward."""

from datetime import datetime, timedelta, timezone

from .db import Store

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
_OFFSET_KEY = "clock_offset_seconds"


def now(store: Store) -> datetime:
    offset = float(store.get_setting(_OFFSET_KEY, "0") or 0)
    return datetime.now(IST).replace(microsecond=0) + timedelta(seconds=offset)


def advance(store: Store, hours: float) -> datetime:
    offset = float(store.get_setting(_OFFSET_KEY, "0") or 0) + hours * 3600
    store.set_setting(_OFFSET_KEY, str(offset))
    return now(store)


def reset(store: Store) -> datetime:
    store.set_setting(_OFFSET_KEY, "0")
    return now(store)


def offset_hours(store: Store) -> float:
    return float(store.get_setting(_OFFSET_KEY, "0") or 0) / 3600


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def iso(value: datetime) -> str:
    return value.astimezone(IST).isoformat(timespec="seconds")

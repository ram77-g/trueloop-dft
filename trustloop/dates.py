"""Turns promise phrases ("by Thursday", "in 5-7 working days", "within 24 hours") into due times."""

import re
from calendar import monthrange
from datetime import datetime, time, timedelta

END_OF_DAY = time(18, 0)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-eight": 48, "seventy-two": 72,
}
_NUM = r"(\d+|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")"
_RANGE = _NUM + r"\s*(?:to|-|–|—|or)\s*" + _NUM


def _number(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token.lower()]


def add_working_days(start: datetime, days: int) -> datetime:
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _at(day: datetime, at: time) -> datetime:
    return day.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


def _time_of_day(text: str) -> time | None:
    if re.search(r"\bnoon\b", text):
        return time(12, 0)
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).startswith("p"):
        hour += 12
    return time(hour, int(match.group(2) or 0))


def resolve(text: str, base: datetime) -> tuple[datetime | None, str]:
    """Return (due time, the phrase that set it), or (None, "") when the text names no time."""
    t = text.lower()
    at = _time_of_day(t)
    date_at = at or END_OF_DAY

    rules = [
        (_RANGE + r"\s*(?:working|business)\s*days?",
         lambda m: _at(add_working_days(base, _number(m.group(2))), date_at)),
        (_NUM + r"\s*(?:working|business)\s*days?",
         lambda m: _at(add_working_days(base, _number(m.group(1))), date_at)),
        (_RANGE + r"\s*hours?", lambda m: base + timedelta(hours=_number(m.group(2)))),
        (_NUM + r"\s*hours?", lambda m: base + timedelta(hours=_number(m.group(1)))),
        (_RANGE + r"\s*days?", lambda m: _at(base + timedelta(days=_number(m.group(2))), date_at)),
        (_NUM + r"\s*days?", lambda m: _at(base + timedelta(days=_number(m.group(1))), date_at)),
        (_NUM + r"\s*weeks?", lambda m: _at(base + timedelta(weeks=_number(m.group(1))), date_at)),
        (r"day after tomorrow", lambda m: _at(base + timedelta(days=2), date_at)),
        (r"\btomorrow\b", lambda m: _at(base + timedelta(days=1), date_at)),
        (r"\b(?:today|tonight|end of (?:the )?day|eod)\b", lambda m: _today(base, date_at)),
        (r"\b(?:end of (?:the )?week|this week)\b", lambda m: _end_of_week(base, date_at)),
        (r"\bnext week\b", lambda m: _at(base + timedelta(days=7), date_at)),
        (r"\b(?:end of (?:the )?month|this month)\b",
         lambda m: _at(base.replace(day=monthrange(base.year, base.month)[1]), date_at)),
        (r"\b(" + "|".join(WEEKDAYS) + r")\b", lambda m: _next_weekday(base, m.group(1), date_at)),
    ]
    for pattern, build in rules:
        match = re.search(pattern, t)
        if match:
            return build(match), match.group(0)
    if at is not None:
        due = _at(base, at)
        return (due if due > base else due + timedelta(days=1)), at.strftime("%H:%M")
    return None, ""


def _today(base: datetime, at: time) -> datetime:
    due = _at(base, at)
    return due if due > base else base + timedelta(hours=2)


def _end_of_week(base: datetime, at: time) -> datetime:
    days_to_friday = (4 - base.weekday()) % 7
    return _at(base + timedelta(days=days_to_friday), at)


def _next_weekday(base: datetime, name: str, at: time) -> datetime:
    days_ahead = (WEEKDAYS.index(name) - base.weekday()) % 7 or 7
    return _at(base + timedelta(days=days_ahead), at)

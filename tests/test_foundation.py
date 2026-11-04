from datetime import datetime

import pytest

from trustloop import bank, clock, policy
from trustloop.clock import IST
from trustloop.dates import add_working_days, resolve

# Wednesday 4 Nov 2026, 10:00 IST
BASE = datetime(2026, 11, 4, 10, 0, tzinfo=IST)


@pytest.mark.parametrize("phrase, expected", [
    ("I'll call you back on Thursday", datetime(2026, 11, 5, 18, 0, tzinfo=IST)),
    ("I'll call you back Thursday at 3 pm", datetime(2026, 11, 5, 15, 0, tzinfo=IST)),
    ("we'll call you on wednesday", datetime(2026, 11, 11, 18, 0, tzinfo=IST)),
    ("your new card will arrive in 2 days", datetime(2026, 11, 6, 18, 0, tzinfo=IST)),
    ("it arrives in two days", datetime(2026, 11, 6, 18, 0, tzinfo=IST)),
    ("within 5-7 working days", datetime(2026, 11, 13, 18, 0, tzinfo=IST)),
    ("in 5 business days", datetime(2026, 11, 11, 18, 0, tzinfo=IST)),
    ("within 24 hours", datetime(2026, 11, 5, 10, 0, tzinfo=IST)),
    ("I'll email the form today", datetime(2026, 11, 4, 18, 0, tzinfo=IST)),
    ("tomorrow by noon", datetime(2026, 11, 5, 12, 0, tzinfo=IST)),
    ("by the end of the week", datetime(2026, 11, 6, 18, 0, tzinfo=IST)),
    ("next week", datetime(2026, 11, 11, 18, 0, tzinfo=IST)),
])
def test_resolve_phrases(phrase, expected):
    due, matched = resolve(phrase, BASE)
    assert due == expected
    assert matched


def test_resolve_without_a_time():
    assert resolve("I'll look into it for you", BASE) == (None, "")


def test_working_days_skip_weekends():
    friday = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    assert add_working_days(friday, 1).date().isoformat() == "2026-11-09"


def test_policy_flags_fast_replacement_and_refund():
    assert "5–7 working days" in policy.check_promise("replacement", BASE, resolve("in 2 days", BASE)[0])
    assert policy.check_promise("replacement", BASE, resolve("in 7 working days", BASE)[0]) == ""
    assert policy.check_promise("refund", BASE, resolve("tomorrow", BASE)[0]) != ""


def test_policy_flags_sunday_and_late_callbacks():
    assert "Sunday" in policy.check_promise("callback", BASE, resolve("sunday", BASE)[0])
    assert "9:00" in policy.check_promise("callback", BASE, resolve("thursday at 8 pm", BASE)[0])
    assert policy.check_promise("callback", BASE, resolve("thursday at 11 am", BASE)[0]) == ""


def test_seed_is_idempotent(store):
    bank.seed(store)
    assert len(store.query("SELECT * FROM customers")) == 10


def test_verify(store):
    ok = bank.verify(store, "+91 98450 01005", "6604", "1995-05-09")
    assert ok == {"verified": True, "customer_id": "C005", "name": "Meera Nair"}
    assert bank.verify(store, "9845001005", "0000", "1995-05-09")["verified"] is False
    assert bank.verify(store, "123", "6604", "1995-05-09")["verified"] is False


def test_duplicate_replacement_is_charged_again(store):
    priya = bank.find_customer(store, customer_id="C003")
    status = bank.card_status(store, priya)
    assert status["replacement"]["status"] == "in_transit"
    result = bank.request_replacement(store, priya, address_confirmed=True)
    assert "already in transit" in result["warning"]
    assert result["fee_inr"] == 200


def test_stolen_replacement_is_free(store):
    meera = bank.find_customer(store, customer_id="C005")
    assert bank.request_replacement(store, meera, True, reason="stolen")["fee_inr"] == 0


def test_declines_carry_explanations(store):
    rohit = bank.find_customer(store, customer_id="C002")
    decline = bank.recent_declines(store, rohit)["declines"][0]
    assert decline["reason"] == "insufficient_funds"
    assert "balance" in decline["explanation"]


def test_clock_advance_and_reset(store):
    start = clock.now(store)
    later = clock.advance(store, 48)
    assert (later - start).total_seconds() >= 48 * 3600
    assert clock.offset_hours(store) == 48
    clock.reset(store)
    assert clock.offset_hours(store) == 0

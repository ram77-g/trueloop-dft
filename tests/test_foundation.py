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
    assert "7 working days" in policy.check_promise("replacement", BASE, resolve("in 2 days", BASE)[0])
    assert policy.check_promise("replacement", BASE, resolve("in 7 working days", BASE)[0]) == ""
    assert policy.check_promise("replacement", BASE, resolve("in 5 working days", BASE)[0]) != ""
    assert policy.check_promise("refund", BASE, resolve("tomorrow", BASE)[0]) != ""
    assert policy.check_promise("refund", BASE, resolve("within 5 working days", BASE)[0]) == ""


def test_policy_flags_callbacks_with_a_time_slot():
    thursday = resolve("thursday", BASE)[0]
    assert "no specific time slot" in policy.check_promise("callback", BASE, thursday, "I'll call you at 3 pm")
    assert policy.check_promise("callback", BASE, thursday, "I'll call you back on Thursday") == ""


def test_seed_loads_person_a_customers_and_extras(store):
    bank.seed(store)
    ids = {r["customer_id"] for r in store.query("SELECT customer_id FROM customers")}
    assert {"C1001", "C1002", "C1003", "C1004", "C1005", "C1006"} <= ids and len(ids) == 16
    names = [r["name"] for r in store.query("SELECT name FROM customers")]
    assert len(names) == len(set(names)), "customer names must be unique"


@pytest.mark.parametrize("dob", ["1999-11-02", "2 Nov 1999", "2nd November 1999", "02/11/1999", "Nov 2, 1999"])
def test_verify_accepts_dates_as_people_type_them(store, dob):
    result = bank.verify(store, "+91 98450 33333", "5562", dob)
    assert result == {"verified": True, "customer_id": "C1003", "card_id": "CARD-5562", "first_name": "Ananya",
                      "name": "Ananya Rao"}


def test_verify_reveals_nothing_on_wrong_details(store):
    wrong_card = bank.verify(store, "+919845066666", "1111", "18 Dec 1985")
    unknown_phone = bank.verify(store, "+910000000000", "2290", "18 Dec 1985")
    assert wrong_card == unknown_phone == {"verified": False, "reason": "The details don't match our records."}


def test_find_by_card_id_including_the_replacement_card(store):
    assert bank.find_customer(store, card_id="CARD-5562")["customer_id"] == "C1003"
    assert bank.find_customer(store, card_id="5562")["customer_id"] == "C1003"
    assert bank.find_customer(store, card_id="CARD-7311")["customer_id"] == "C1002"


def test_card_status_matches_person_a_data(store):
    rahul = bank.find_customer(store, customer_id="C1002")
    status = bank.card_status(store, rahul)
    assert status["status"] == "blocked (damaged)"
    assert status["replacement"] == {"card_id": "CARD-7311", "status": "dispatched",
                                     "requested_at": "2026-10-30T12:00:00+05:30", "eta": "2026-11-10"}
    assert bank.card_status(store, bank.find_customer(store, customer_id="C1006"))["international_enabled"] is True


def test_duplicate_replacement_is_charged_again(store):
    rahul = bank.find_customer(store, customer_id="C1002")
    result = bank.request_replacement(store, rahul, address_confirmed=True)
    assert "already dispatched" in result["warning"] and result["fee_inr"] == 200
    assert result["new_card_id"] == "CARD-7312"


def test_new_replacement_takes_seven_working_days(store):
    sneha = bank.find_customer(store, customer_id="C1005")
    result = bank.request_replacement(store, sneha, address_confirmed=True)
    expected = add_working_days(clock.now(store), 7).date().isoformat()
    assert result["eta"] == expected and "warning" not in result and result["new_card_id"] == "CARD-6618"


def test_declines_in_the_contract_shape(store):
    priya = bank.find_customer(store, customer_id="C1001")
    assert bank.recent_declines(store, priya)["declines"] == [
        {"date": "2026-11-03", "merchant": "Croma, Indiranagar", "amount_inr": 18499, "reason": "Incorrect PIN entered"}]


def test_old_database_is_rebuilt(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers VALUES ('X', 'Old')")
    from trustloop import db
    s = db.Store(str(path))
    bank.seed(s)
    assert s.query_one("SELECT COUNT(*) AS n FROM customers")["n"] == 16
    s.close()


def test_clock_advance_and_reset(store):
    start = clock.now(store)
    later = clock.advance(store, 48)
    assert (later - start).total_seconds() >= 48 * 3600
    assert clock.offset_hours(store) == 48
    clock.reset(store)
    assert clock.offset_hours(store) == 0

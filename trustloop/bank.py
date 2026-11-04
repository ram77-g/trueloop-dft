"""The mock Nimbus Bank behind CardCare's tools."""

import json
import re
from datetime import timedelta

from . import clock
from .config import DATA_DIR
from .dates import add_working_days
from .db import Store

REPLACEMENT_FEE = 200
INTERNATIONAL_MAX_DAYS = 90
DECLINE_REASONS = {
    "insufficient_funds": "The account did not have enough balance for this payment.",
    "online_transactions_disabled": "Online transactions are switched off. The customer can enable them in the Nimbus app.",
    "suspected_fraud_check": "The payment was held by a fraud check. A specialist should review it.",
}


def load_customers() -> list[dict]:
    return json.loads((DATA_DIR / "customers.json").read_text(encoding="utf-8"))


def seed(store: Store) -> None:
    """Load the mock customers. Safe to call again: existing customers are left alone."""
    if store.query_one("SELECT 1 AS x FROM customers"):
        return
    now = clock.now(store)
    with store.transaction():
        for c in load_customers():
            store.execute(
                "INSERT INTO customers (customer_id, name, phone, last4, dob, email) VALUES (?, ?, ?, ?, ?, ?)",
                (c["customer_id"], c["name"], c["phone"], c["last4"], c["dob"], c["email"]),
            )
            card = c["card"]
            replacement = card.get("replacement")
            requested = eta = None
            if replacement:
                requested_at = now - timedelta(days=replacement["requested_working_days_ago"] * 7 / 5)
                requested = clock.iso(requested_at)
                eta = clock.iso(add_working_days(requested_at, 7))
            store.execute(
                "INSERT INTO cards (customer_id, last4, status, replacement_status, replacement_requested_at, replacement_eta) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (c["customer_id"], c["last4"], card["status"],
                 replacement["status"] if replacement else None, requested, eta),
            )
            for d in c.get("declines", []):
                store.execute(
                    "INSERT INTO declines (customer_id, at, merchant, amount, reason) VALUES (?, ?, ?, ?, ?)",
                    (c["customer_id"], clock.iso(now - timedelta(hours=d["hours_ago"])),
                     d["merchant"], d["amount"], d["reason"]),
                )


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def find_customer(store: Store, phone: str = "", customer_id: str = "") -> dict | None:
    if customer_id:
        return store.query_one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
    digits = _digits(phone)
    if len(digits) < 10:
        return None
    for row in store.query("SELECT * FROM customers"):
        if _digits(row["phone"]) == digits:
            return row
    return None


def record_event(store: Store, customer_id: str, kind: str, detail: str = "") -> dict:
    at = clock.iso(clock.now(store))
    event_id = store.execute(
        "INSERT INTO bank_events (at, customer_id, kind, detail) VALUES (?, ?, ?, ?)",
        (at, customer_id, kind, detail),
    )
    return {"id": event_id, "at": at, "customer_id": customer_id, "kind": kind, "detail": detail}


def verify(store: Store, phone: str, last4: str, dob: str) -> dict:
    customer = find_customer(store, phone=phone)
    if not customer:
        return {"verified": False, "reason": "No customer with that phone number."}
    if str(last4).strip()[-4:] != customer["last4"] or str(dob).strip() != customer["dob"]:
        return {"verified": False, "reason": "The card digits or date of birth do not match."}
    return {"verified": True, "customer_id": customer["customer_id"], "name": customer["name"]}


def card_status(store: Store, customer: dict) -> dict:
    card = store.query_one("SELECT * FROM cards WHERE customer_id = ?", (customer["customer_id"],))
    result = {
        "customer_id": customer["customer_id"],
        "card_last4": card["last4"],
        "status": card["status"],
        "international_usage": bool(card["international"]),
        "international_until": card["international_until"],
        "replacement": None,
    }
    if card["replacement_status"]:
        result["replacement"] = {
            "status": card["replacement_status"],
            "requested_at": card["replacement_requested_at"],
            "expected_by": card["replacement_eta"],
        }
    return result


def recent_declines(store: Store, customer: dict) -> dict:
    rows = store.query(
        "SELECT at, merchant, amount, reason FROM declines WHERE customer_id = ? ORDER BY at DESC",
        (customer["customer_id"],),
    )
    for row in rows:
        row["explanation"] = DECLINE_REASONS.get(row["reason"], "")
    return {"customer_id": customer["customer_id"], "declines": rows}


def block(store: Store, customer: dict, reason: str) -> dict:
    store.execute("UPDATE cards SET status = 'blocked' WHERE customer_id = ?", (customer["customer_id"],))
    record_event(store, customer["customer_id"], "card_blocked", reason)
    return {"blocked": True, "customer_id": customer["customer_id"], "reason": reason}


def request_replacement(store: Store, customer: dict, address_confirmed: bool, reason: str = "") -> dict:
    card = store.query_one("SELECT * FROM cards WHERE customer_id = ?", (customer["customer_id"],))
    duplicate = card["replacement_status"] == "in_transit"
    now = clock.now(store)
    eta = add_working_days(now, 7)
    fee = 0 if "stolen" in (reason or "").lower() else REPLACEMENT_FEE
    store.execute(
        "UPDATE cards SET replacement_status = 'in_transit', replacement_requested_at = ?, replacement_eta = ? "
        "WHERE customer_id = ?",
        (clock.iso(now), clock.iso(eta), customer["customer_id"]),
    )
    record_event(store, customer["customer_id"], "replacement_requested",
                 "duplicate order" if duplicate else (reason or "replacement"))
    result = {
        "ordered": True,
        "customer_id": customer["customer_id"],
        "fee_inr": fee,
        "address_confirmed": bool(address_confirmed),
        "delivery_window": "5-7 working days",
        "expected_by": clock.iso(eta),
    }
    if duplicate:
        result["warning"] = ("A replacement was already in transit. A second card was ordered and the "
                             f"fee of ₹{fee} was charged again.")
    return result


def set_international(store: Store, customer: dict, enabled: bool, days: int = INTERNATIONAL_MAX_DAYS) -> dict:
    days = max(1, min(int(days or INTERNATIONAL_MAX_DAYS), INTERNATIONAL_MAX_DAYS))
    until = clock.iso(clock.now(store) + timedelta(days=days)) if enabled else None
    store.execute(
        "UPDATE cards SET international = ?, international_until = ? WHERE customer_id = ?",
        (1 if enabled else 0, until, customer["customer_id"]),
    )
    record_event(store, customer["customer_id"], "international_" + ("enabled" if enabled else "disabled"))
    return {"customer_id": customer["customer_id"], "international_usage": bool(enabled), "until": until}


# Events that the console uses to show a promise being kept.
SIMULATED_EVENTS = {
    "replacement_delivered": "The replacement card was delivered.",
    "refund_paid": "The refund reached the customer's account.",
    "callback_completed": "An agent called the customer back.",
    "document_sent": "The promised document was emailed.",
}


def simulate(store: Store, customer_id: str, kind: str) -> dict:
    if kind not in SIMULATED_EVENTS:
        raise ValueError(f"Unknown event {kind!r}")
    if kind == "replacement_delivered":
        store.execute("UPDATE cards SET replacement_status = 'delivered', status = 'active' WHERE customer_id = ?",
                      (customer_id,))
    return record_event(store, customer_id, kind, SIMULATED_EVENTS[kind])

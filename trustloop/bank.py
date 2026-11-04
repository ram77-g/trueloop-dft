"""The mock Tarang Bank behind CardCare's tools (a fictional bank).

Customers come from two files: Person A's contract file (tarang_customers.json, the customers the scripted
Talkdesk chats use) and a few extra demo customers (customers.json)."""

import json
import re
from datetime import datetime, timedelta

from . import clock
from .config import DATA_DIR
from .dates import add_working_days
from .db import Store

BANK_NAME = "Tarang Bank"
REPLACEMENT_FEE = 200
REPLACEMENT_WORKING_DAYS = 7
INTERNATIONAL_MAX_DAYS = 90
IN_PROGRESS = ("in_transit", "dispatched", "ordered", "requested")


def _email(name: str) -> str:
    return ".".join(name.lower().split()) + "@example.com"


def _at(date_text: str, hour: int = 12) -> str:
    return clock.iso(datetime.fromisoformat(date_text).replace(hour=hour, tzinfo=clock.IST))


def load_customers() -> list[dict]:
    """Every customer, normalised to one shape."""
    out = []
    contract = json.loads((DATA_DIR / "tarang_customers.json").read_text(encoding="utf-8"))
    for c in contract["customers"]:
        replacement = c.get("replacement")
        out.append({
            "customer_id": c["customer_id"], "name": c["name"], "phone": c["phone"], "last4": c["last4"],
            "dob": c["dob"], "email": _email(c["name"]), "address": c.get("address", ""),
            "card_id": c.get("card_id") or f"CARD-{c['last4']}",
            "status": c.get("card_status", "active"), "international": bool(c.get("international_enabled")),
            "replacement": replacement and {
                "card_id": replacement.get("card_id"), "status": replacement["status"],
                "requested_at": _at(replacement["requested_on"]) if replacement.get("requested_on") else None,
                "eta": _at(replacement["eta"], 18) if replacement.get("eta") else None,
            },
            "declines": [{"at": _at(d["date"]), "merchant": d["merchant"], "amount": d["amount_inr"],
                          "reason": d["reason"]} for d in c.get("recent_declines", [])],
        })
    for c in json.loads((DATA_DIR / "customers.json").read_text(encoding="utf-8")):
        out.append({
            "customer_id": c["customer_id"], "name": c["name"], "phone": c["phone"], "last4": c["last4"],
            "dob": c["dob"], "email": c["email"], "address": c.get("address", ""),
            "card_id": f"CARD-{c['last4']}", "status": c["card"]["status"], "international": False,
            "replacement": c["card"].get("replacement"), "declines": c.get("declines", []),
        })
    return out


def seed(store: Store) -> None:
    """Load the mock customers. Safe to call again: existing customers are left alone."""
    if store.query_one("SELECT 1 AS x FROM customers"):
        return
    now = clock.now(store)
    with store.transaction():
        for c in load_customers():
            store.execute(
                "INSERT INTO customers (customer_id, name, phone, last4, dob, email, address) VALUES (?,?,?,?,?,?,?)",
                (c["customer_id"], c["name"], c["phone"], c["last4"], c["dob"], c["email"], c["address"]),
            )
            r = c["replacement"]
            if r and "requested_working_days_ago" in r:  # relative dates in the extra demo customers
                requested_at = now - timedelta(days=r["requested_working_days_ago"] * 7 / 5)
                r = {"card_id": None, "status": r["status"], "requested_at": clock.iso(requested_at),
                     "eta": clock.iso(add_working_days(requested_at, REPLACEMENT_WORKING_DAYS))}
            store.execute(
                "INSERT INTO cards (customer_id, card_id, last4, status, international, replacement_card_id, "
                "replacement_status, replacement_requested_at, replacement_eta) VALUES (?,?,?,?,?,?,?,?,?)",
                (c["customer_id"], c["card_id"], c["last4"], c["status"], int(c["international"]),
                 r and (r["card_id"] or _next_card_id(c["card_id"])), r and r["status"],
                 r and r["requested_at"], r and r["eta"]),
            )
            for d in c["declines"]:
                at = d.get("at") or clock.iso(now - timedelta(hours=d["hours_ago"]))
                store.execute("INSERT INTO declines (customer_id, at, merchant, amount, reason) VALUES (?,?,?,?,?)",
                              (c["customer_id"], at, d["merchant"], d["amount"], d["reason"]))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _next_card_id(card_id: str) -> str:
    digits = _digits(card_id)[-4:] or "0000"
    return f"CARD-{(int(digits) + 1) % 10000:04d}"


DOB_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
               "%Y/%m/%d", "%d%b%Y", "%d%B%Y")


def parse_dob(text: str) -> str:
    """Accept a date of birth the way customers type it ("2 Nov 1999", "02/11/1999") and return YYYY-MM-DD."""
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", (text or "").strip(), flags=re.I).replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    for fmt in DOB_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return cleaned


def find_customer(store: Store, phone: str = "", customer_id: str = "", card_id: str = "") -> dict | None:
    """Look a customer up by id, card id (CARD-5562, or just 5562) or phone number."""
    if customer_id:
        row = store.query_one("SELECT * FROM customers WHERE upper(customer_id) = upper(?)", (customer_id.strip(),))
        if row:
            return row
    if card_id:
        digits = _digits(card_id)[-4:]
        row = store.query_one(
            "SELECT c.* FROM customers c JOIN cards k ON k.customer_id = c.customer_id "
            "WHERE upper(k.card_id) = upper(?) OR upper(k.replacement_card_id) = upper(?) OR k.last4 = ?",
            (card_id.strip(), card_id.strip(), digits),
        )
        if row:
            return row
    digits = _digits(phone)[-10:]
    if len(digits) == 10:
        for row in store.query("SELECT * FROM customers"):
            if _digits(row["phone"])[-10:] == digits:
                return row
    return None


def record_event(store: Store, customer_id: str, kind: str, detail: str = "") -> dict:
    at = clock.iso(clock.now(store))
    event_id = store.execute(
        "INSERT INTO bank_events (at, customer_id, kind, detail) VALUES (?, ?, ?, ?)",
        (at, customer_id, kind, detail),
    )
    return {"id": event_id, "at": at, "customer_id": customer_id, "kind": kind, "detail": detail}


def _card(store: Store, customer: dict) -> dict:
    return store.query_one("SELECT * FROM cards WHERE customer_id = ?", (customer["customer_id"],))


def verify(store: Store, phone: str, last4: str, dob: str) -> dict:
    customer = find_customer(store, phone=phone)
    if not customer or _digits(last4)[-4:] != customer["last4"] or parse_dob(dob) != customer["dob"]:
        # Same answer whatever was wrong, so callers can't probe who has an account.
        return {"verified": False, "reason": "The details don't match our records."}
    card = _card(store, customer)
    return {"verified": True, "customer_id": customer["customer_id"], "card_id": card["card_id"],
            "first_name": customer["name"].split()[0], "name": customer["name"]}


def card_status(store: Store, customer: dict) -> dict:
    card = _card(store, customer)
    result = {
        "customer_id": customer["customer_id"],
        "card_id": card["card_id"],
        "card_last4": card["last4"],
        "status": card["status"],
        "international_enabled": bool(card["international"]),
        "international_until": card["international_until"],
        "replacement": None,
    }
    if card["replacement_status"]:
        result["replacement"] = {
            "card_id": card["replacement_card_id"],
            "status": card["replacement_status"],
            "requested_at": card["replacement_requested_at"],
            "eta": (card["replacement_eta"] or "")[:10],
        }
    return result


def recent_declines(store: Store, customer: dict) -> dict:
    rows = store.query("SELECT at, merchant, amount, reason FROM declines WHERE customer_id = ? ORDER BY at DESC",
                       (customer["customer_id"],))
    return {"customer_id": customer["customer_id"],
            "declines": [{"date": r["at"][:10], "merchant": r["merchant"], "amount_inr": r["amount"],
                          "reason": r["reason"]} for r in rows]}


def block(store: Store, customer: dict, reason: str) -> dict:
    card = _card(store, customer)
    store.execute("UPDATE cards SET status = 'blocked' WHERE customer_id = ?", (customer["customer_id"],))
    record_event(store, customer["customer_id"], "card_blocked", reason)
    return {"blocked": True, "card_id": card["card_id"], "reason": (reason or "").upper()}


def request_replacement(store: Store, customer: dict, address_confirmed: bool, reason: str = "") -> dict:
    card = _card(store, customer)
    duplicate = (card["replacement_status"] or "").lower() in IN_PROGRESS
    now = clock.now(store)
    eta = add_working_days(now, REPLACEMENT_WORKING_DAYS).replace(hour=18, minute=0, second=0)
    new_card_id = _next_card_id(card["replacement_card_id"] or card["card_id"])
    fee = 0 if "stolen" in (reason or "").lower() else REPLACEMENT_FEE
    store.execute(
        "UPDATE cards SET replacement_card_id = ?, replacement_status = 'ordered', replacement_requested_at = ?, "
        "replacement_eta = ? WHERE customer_id = ?",
        (new_card_id, clock.iso(now), clock.iso(eta), customer["customer_id"]),
    )
    record_event(store, customer["customer_id"], "replacement_requested",
                 "duplicate order" if duplicate else (reason or "replacement"))
    result = {
        "ordered": True,
        "new_card_id": new_card_id,
        "fee_inr": fee,
        "address_confirmed": bool(address_confirmed),
        "delivery": f"{REPLACEMENT_WORKING_DAYS} working days",
        "eta": eta.date().isoformat(),
    }
    if duplicate:
        result["warning"] = (f"A replacement ({card['replacement_card_id']}) was already {card['replacement_status']}. "
                             f"A second card was ordered and the ₹{fee} fee was charged again.")
    return result


def set_international(store: Store, customer: dict, enabled: bool, days: int = INTERNATIONAL_MAX_DAYS) -> dict:
    days = max(1, min(int(days or INTERNATIONAL_MAX_DAYS), INTERNATIONAL_MAX_DAYS))
    until = clock.iso(clock.now(store) + timedelta(days=days)) if enabled else None
    store.execute(
        "UPDATE cards SET international = ?, international_until = ? WHERE customer_id = ?",
        (1 if enabled else 0, until, customer["customer_id"]),
    )
    record_event(store, customer["customer_id"], "international_" + ("enabled" if enabled else "disabled"))
    return {"card_id": _card(store, customer)["card_id"], "international_enabled": bool(enabled),
            "until": until and until[:10]}


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

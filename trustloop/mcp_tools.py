"""CardCare's tools, served to Talkdesk over MCP. Every call is logged for Promise Keeper and Mender."""

import re
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import bank, clock, promises
from .config import load_settings
from .db import dumps, get_store
from .policy import PROMISE_KINDS

AGENT_NAME = "CardCare"

INSTRUCTIONS = (
    "Tools for Nimbus Bank debit card support (TrustLoop demo). Identify customers by phone number. "
    "Pass the conversation or interaction id as session_id when you have one."
)


def _session(session_id: str, phone: str) -> str:
    if session_id:
        return session_id
    now = clock.now(get_store())
    digits = re.sub(r"\D", "", phone or "")[-10:] or "anon"
    return f"auto-{digits}-{now:%Y%m%d%H}{now.minute // 30}"


def _log(session_id: str, customer_id: str | None, tool: str, args: dict, result: dict) -> dict:
    store = get_store()
    store.execute(
        "INSERT INTO tool_calls (at, session_id, customer_id, tool, args, result) VALUES (?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), session_id, customer_id, tool, dumps(args), dumps(result)),
    )
    return result


def _customer(phone: str) -> dict | None:
    return bank.find_customer(get_store(), phone=phone)


NOT_FOUND = {"error": "No customer with that phone number. Ask the customer to confirm it."}


def verify_customer(phone: str, last4: str, dob: str, session_id: str = "") -> dict:
    """Verify a customer's identity with their phone number, the last 4 digits of their debit card and their
    date of birth (YYYY-MM-DD). Returns verified true or false."""
    sid = _session(session_id, phone)
    result = bank.verify(get_store(), phone, last4, dob)
    return _log(sid, result.get("customer_id"), "verify_customer",
                {"phone": phone, "last4": last4, "dob": dob}, result)


def get_card_status(phone: str, session_id: str = "") -> dict:
    """Get the status of the customer's debit card: active or blocked, international usage, and any
    replacement card in transit with its expected delivery date."""
    sid, customer = _session(session_id, phone), _customer(phone)
    result = bank.card_status(get_store(), customer) if customer else NOT_FOUND
    return _log(sid, customer and customer["customer_id"], "get_card_status", {"phone": phone}, result)


def get_recent_declines(phone: str, session_id: str = "") -> dict:
    """List the customer's recently declined card payments with the reason for each."""
    sid, customer = _session(session_id, phone), _customer(phone)
    result = bank.recent_declines(get_store(), customer) if customer else NOT_FOUND
    return _log(sid, customer and customer["customer_id"], "get_recent_declines", {"phone": phone}, result)


def block_card(phone: str, reason: str, session_id: str = "") -> dict:
    """Block the customer's debit card immediately, e.g. when it is lost or stolen."""
    sid, customer = _session(session_id, phone), _customer(phone)
    result = bank.block(get_store(), customer, reason) if customer else NOT_FOUND
    return _log(sid, customer and customer["customer_id"], "block_card", {"phone": phone, "reason": reason}, result)


def request_replacement(phone: str, address_confirmed: bool, reason: str = "", session_id: str = "") -> dict:
    """Order a replacement debit card to the customer's registered address. reason is e.g. lost, stolen
    or damaged."""
    sid, customer = _session(session_id, phone), _customer(phone)
    result = bank.request_replacement(get_store(), customer, address_confirmed, reason) if customer else NOT_FOUND
    return _log(sid, customer and customer["customer_id"], "request_replacement",
                {"phone": phone, "address_confirmed": address_confirmed, "reason": reason}, result)


def set_international_usage(phone: str, enabled: bool, days: int = 90, session_id: str = "") -> dict:
    """Turn international usage of the debit card on or off. When turning it on, days is how long it
    stays on (at most 90)."""
    sid, customer = _session(session_id, phone), _customer(phone)
    result = bank.set_international(get_store(), customer, enabled, days) if customer else NOT_FOUND
    return _log(sid, customer and customer["customer_id"], "set_international_usage",
                {"phone": phone, "enabled": enabled, "days": days}, result)


def record_promise(phone: str, promise: str, kind: str, due: str = "", session_id: str = "") -> dict:
    """Record a promise you made to the customer, in your own words, e.g. "Your new card will arrive in
    2 days". kind is one of callback, refund, document, replacement, international, follow_up. due is when
    it will happen, as you said it to the customer or as an ISO date-time."""
    store, settings = get_store(), load_settings()
    sid, customer = _session(session_id, phone), _customer(phone)
    created_at = clock.now(store)
    iso_due = ""
    if due:
        try:
            iso_due = clock.iso(datetime.fromisoformat(due.strip()).replace(tzinfo=clock.IST)) \
                if "T" in due or re.match(r"^\d{4}-\d{2}-\d{2}", due.strip()) else ""
        except ValueError:
            iso_due = ""
    saved = promises.save_promise(
        store,
        {"quote": promise, "kind": kind if kind in PROMISE_KINDS else "follow_up", "description": promise,
         "due_phrase": "" if iso_due else due, "due_at": iso_due},
        created_at=created_at, session_id=sid, customer_id=customer and customer["customer_id"],
        made_by=AGENT_NAME, agent_type="ai", source="mcp",
    )
    result = {"recorded": True, "promise_id": saved["id"], "due_at": saved["due_at"]}
    if settings.live_guard and saved["policy_violation"]:
        result["policy_warning"] = saved["policy_violation"] + " Correct what you told the customer."
    return _log(sid, customer and customer["customer_id"], "record_promise",
                {"phone": phone, "promise": promise, "kind": kind, "due": due}, result)


def record_outcome(intent: str, resolved: bool, escalated: bool, reason: str = "", phone: str = "",
                   session_id: str = "") -> dict:
    """Record how the conversation ended: the customer's intent (e.g. lost_card, card_declined,
    international_usage, replacement_status), whether it was resolved, and whether you handed over to a
    human agent."""
    store = get_store()
    sid, customer = _session(session_id, phone), _customer(phone)
    store.execute(
        "INSERT INTO outcomes (at, session_id, customer_id, intent, resolved, escalated, reason) VALUES (?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), sid, customer and customer["customer_id"], intent, int(resolved),
         int(escalated), reason),
    )
    return _log(sid, customer and customer["customer_id"], "record_outcome",
                {"intent": intent, "resolved": resolved, "escalated": escalated, "reason": reason, "phone": phone},
                {"recorded": True})


def get_customer_promises(phone: str, session_id: str = "") -> dict:
    """See what Nimbus Bank has already promised this customer, including promises that were broken, so
    you can acknowledge them."""
    sid = _session(session_id, phone)
    card = promises.card(get_store(), load_settings(), phone=phone)
    result = NOT_FOUND if card is None else {
        "headline": card["headline"],
        "broken": [{"promise": p["description"], "was_due": p["due_at"]} for p in card["broken"]],
        "open": [{"promise": p["description"], "due": p["due_at"]} for p in card["open"]],
    }
    return _log(sid, card and card["customer"]["customer_id"], "get_customer_promises", {"phone": phone}, result)


TOOLS = {f.__name__: f for f in (
    verify_customer, get_card_status, get_recent_declines, block_card, request_replacement,
    set_international_usage, record_promise, record_outcome, get_customer_promises,
)}


def build_mcp() -> FastMCP:
    server = FastMCP(
        "TrustLoop CardCare",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # The server sits behind a public host name, so host-header pinning would reject Talkdesk.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for fn in TOOLS.values():
        server.add_tool(fn)
    return server

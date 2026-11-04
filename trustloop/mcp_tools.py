"""CardCare's tools, served to Talkdesk over MCP. Every call is logged for Promise Keeper and Mender.

The names and parameters follow the contract in Person A's orchestrator.py. Talkdesk sends every parameter as a
string (booleans as 'true'/'false'), so every parameter here is a string. session_id is optional on every tool;
bind it to Talkdesk's Interaction ID so all calls in one conversation group together."""

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
    "Tools for Tarang Bank debit card support (TrustLoop demo, fictional bank). Verify the customer first; "
    "verify_customer returns the customer_id and card_id the other tools need."
)
NOT_FOUND = {"error": "No card or customer matches that id. Verify the customer first to get the right ids."}


def _flag(value) -> bool:
    return str(value).strip().lower() in {"true", "yes", "y", "1", "on"}


def _session(session_id: str, key: str) -> str:
    if session_id and session_id.strip():
        return session_id.strip()
    now = clock.now(get_store())
    tag = re.sub(r"[^A-Za-z0-9]", "", key or "") or "anon"
    return f"auto-{tag}-{now:%Y%m%d%H}{now.minute // 30}"


def _log(session_id: str, customer: dict | None, tool: str, args: dict, result: dict) -> dict:
    store = get_store()
    store.execute(
        "INSERT INTO tool_calls (at, session_id, customer_id, tool, args, result) VALUES (?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), session_id, customer and customer["customer_id"], tool, dumps(args),
         dumps(result)),
    )
    return result


def _by_customer(customer_id: str) -> dict | None:
    return bank.find_customer(get_store(), customer_id=customer_id, card_id=customer_id, phone=customer_id)


def _by_card(card_id: str) -> dict | None:
    return bank.find_customer(get_store(), card_id=card_id, customer_id=card_id)


def verify_customer(phone: str, last4: str, dob: str, session_id: str = "") -> dict:
    """Verify the customer owns the card: registered phone number, last 4 digits of the card and date of birth,
    exactly as the customer gave them. Returns verified true or false and, when verified, customer_id, card_id
    and first_name. Call this before any account lookup."""
    store = get_store()
    result = bank.verify(store, phone, last4, dob)
    customer = bank.find_customer(store, customer_id=result["customer_id"]) if result["verified"] else None
    sid = _session(session_id, result.get("customer_id") or phone)
    return _log(sid, customer, "verify_customer", {"phone": phone, "last4": last4, "dob": dob}, result)


def get_card_status(customer_id: str, session_id: str = "") -> dict:
    """Card status for a verified customer: active or blocked, international usage, and any replacement in
    progress with its card_id, status and expected delivery date (eta)."""
    customer = _by_customer(customer_id)
    result = bank.card_status(get_store(), customer) if customer else NOT_FOUND
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "get_card_status",
                {"customer_id": customer_id}, result)


def get_recent_declines(customer_id: str, session_id: str = "") -> dict:
    """A verified customer's recently declined card payments: date, merchant, amount_inr and reason."""
    customer = _by_customer(customer_id)
    result = bank.recent_declines(get_store(), customer) if customer else NOT_FOUND
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "get_recent_declines",
                {"customer_id": customer_id}, result)


def block_card(card_id: str, reason: str, session_id: str = "") -> dict:
    """Permanently block a debit card. card_id is 'CARD-' followed by the last 4 digits (like CARD-5562).
    reason is one of LOST, STOLEN, DAMAGED, FRAUD."""
    customer = _by_card(card_id)
    result = bank.block(get_store(), customer, reason) if customer else NOT_FOUND
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "block_card",
                {"card_id": card_id, "reason": reason}, result)


def request_replacement(card_id: str, address_confirmed: str, session_id: str = "") -> dict:
    """Order a replacement card to the registered address. address_confirmed is 'true' only after the customer
    confirmed the address. Returns the new card id and the expected delivery date (eta)."""
    customer = _by_card(card_id)
    result = bank.request_replacement(get_store(), customer, _flag(address_confirmed)) if customer else NOT_FOUND
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "request_replacement",
                {"card_id": card_id, "address_confirmed": address_confirmed}, result)


def set_international_usage(card_id: str, enabled: str, session_id: str = "") -> dict:
    """Turn international usage on ('true') or off ('false') for a verified customer's card."""
    customer = _by_card(card_id)
    result = bank.set_international(get_store(), customer, _flag(enabled)) if customer else NOT_FOUND
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "set_international_usage",
                {"card_id": card_id, "enabled": enabled}, result)


def _due_iso(due_at: str) -> str:
    text = (due_at or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if len(text) == 10:  # a date with no time: the end of that business day
        parsed = parsed.replace(hour=18)
    return clock.iso(parsed if parsed.tzinfo else parsed.replace(tzinfo=clock.IST))


def record_promise(promise_text: str, customer_id: str = "", due_at: str = "", session_id: str = "") -> dict:
    """Log a commitment made to the customer (a delivery date, a reversal date, a callback) so TrustLoop Promise
    Keeper can track it. promise_text is the commitment exactly as told to the customer; due_at is the due date
    in YYYY-MM-DD."""
    store, settings = get_store(), load_settings()
    customer = _by_customer(customer_id) if customer_id else None
    sid = _session(session_id, customer and customer["customer_id"])
    iso_due = _due_iso(due_at)
    kinds = promises._kinds_in(promise_text)
    saved = promises.save_promise(
        store,
        {"quote": promise_text, "kind": kinds[0] if kinds and kinds[0] in PROMISE_KINDS else "follow_up",
         "description": promise_text, "due_phrase": "" if iso_due else due_at, "due_at": iso_due},
        created_at=clock.now(store), session_id=sid, customer_id=customer and customer["customer_id"],
        made_by=AGENT_NAME, agent_type="ai", source="mcp",
    )
    result = {"recorded": True, "promise_id": saved["id"], "due_at": saved["due_at"][:10]}
    if settings.live_guard and saved["policy_violation"]:
        result["policy_warning"] = saved["policy_violation"] + " Correct what you told the customer."
    return _log(sid, customer, "record_promise",
                {"customer_id": customer_id, "promise_text": promise_text, "due_at": due_at}, result)


def record_outcome(intent: str, resolved: str, escalated: str, reason: str = "", customer_id: str = "",
                   session_id: str = "") -> dict:
    """Log how the conversation ended: a short intent label (LOST_CARD, DECLINE, REPLACEMENT_STATUS,
    INTERNATIONAL, HUMAN_REQUEST), resolved 'true'/'false', escalated 'true' if handed to a human, and a short
    reason."""
    store = get_store()
    customer = _by_customer(customer_id) if customer_id else None
    sid = _session(session_id, customer and customer["customer_id"])
    store.execute(
        "INSERT INTO outcomes (at, session_id, customer_id, intent, resolved, escalated, reason) VALUES (?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), sid, customer and customer["customer_id"], intent, int(_flag(resolved)),
         int(_flag(escalated)), reason),
    )
    return _log(sid, customer, "record_outcome",
                {"intent": intent, "resolved": resolved, "escalated": escalated, "reason": reason}, {"recorded": True})


def get_customer_promises(customer_id: str, session_id: str = "") -> dict:
    """What Tarang Bank has already promised this customer, including broken promises, so you can acknowledge
    them."""
    customer = _by_customer(customer_id)
    card = promises.card(get_store(), load_settings(), customer_id=customer["customer_id"]) if customer else None
    result = NOT_FOUND if card is None else {
        "headline": card["headline"],
        "broken": [{"promise": p["description"], "was_due": p["due_at"][:10]} for p in card["broken"]],
        "open": [{"promise": p["description"], "due": p["due_at"][:10]} for p in card["open"]],
    }
    return _log(_session(session_id, customer and customer["customer_id"]), customer, "get_customer_promises",
                {"customer_id": customer_id}, result)


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

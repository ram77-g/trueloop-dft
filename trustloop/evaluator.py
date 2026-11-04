"""Runs Person A's test cases against CardCare's instructions, each case in its own copy of the bank.

Cases and scoring follow person_a/phase1/cases.json and cases.py: required tools in order, no forbidden tools,
every must_mention regex matches, no must_not_mention regex matches, and escalation as expected."""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from . import bank, clock, db, mcp_tools
from .config import DATA_DIR
from .llm import LLM, LLMError

log = logging.getLogger("trustloop.evaluator")
MAX_TURNS = 8
ESCALATION = re.compile(
    r"\b(connect|transfer|hand(?:ing)?\s+(?:you\s+)?over|pass(?:ing)?\s+(?:you\s+)?(?:on|over))\w*\s+"
    r"(?:you\s+)?(?:to|with)\s+(?:a|an|our|one of our)?\s*(?:human|agent|specialist|colleague|team member|representative|person)",
    re.IGNORECASE,
)
FAILURE_KINDS = ("seeded", "failure")


def load_cases() -> list[dict]:
    return json.loads((DATA_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]


def customer_message(case: dict) -> str:
    """Turn a scripted chat into one message: what the customer types plus the details they'd give when asked."""
    text = " ".join(case["say"])
    extra = (case.get("if_asked") or "").strip()
    if not extra:
        return text
    if extra.lower().startswith("say "):
        extra = re.sub(r"^say (that )?you are", "I'm", extra, flags=re.I)
        extra = re.sub(r"\byou don't\b", "I don't", extra, flags=re.I)
        return f"{text} {extra[0].upper()}{extra[1:]}"
    if ":" in extra.split(",")[0] and re.search(r"on purpose|wrong", extra.split(":")[0], re.I):
        extra = extra.split(":", 1)[1].strip()
    extra = re.sub(r"If asked about the address: yes, same address\.?", "Please use my registered address.", extra)
    return f"{text} My details: {extra}"


def check_case(case: dict, trace: dict) -> dict:
    """Score one run the way Person A's cases.py does."""
    good = case["good"]
    tools = [t["name"] for t in trace["tools"]]
    text = trace["text"]
    reasons = []
    required = good.get("required_tools", [])
    it = iter(tools)
    if required and not all(any(t == need for t in it) for need in required):
        reasons.append(f"tools: needed {' → '.join(required)} in that order, got {tools or 'none'}")
    bad = [t for t in good.get("forbidden_tools", []) if t in tools]
    if bad:
        reasons.append(f"tools: called forbidden {bad}")
    for pattern in good.get("must_mention", []):
        if not re.search(pattern, text, re.I):
            reasons.append(f"reply: missing /{pattern}/")
    for pattern in good.get("must_not_mention", []):
        match = re.search(pattern, text, re.I)
        if match:
            reasons.append(f"reply: must not say /{pattern}/ (said {match.group(0)!r})")
    want = good.get("should_escalate")
    if want is not None and bool(trace["escalated"]) != want:
        reasons.append(f"escalation: expected {'yes' if want else 'no'}, got {'yes' if trace['escalated'] else 'no'}")
    return {"passed": not reasons, "reasons": reasons}


def _escalated(trace: dict) -> bool:
    for t in trace["tools"]:
        if t["name"] == "record_outcome" and str(t["args"].get("escalated", "")).lower() in ("true", "1", "yes"):
            return True
    return bool(ESCALATION.search(trace["text"]))


def _run_tool(name: str, args: dict, session_id: str) -> dict:
    fn = mcp_tools.TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name}"}
    args = {k: (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower()
                if isinstance(v, bool) else str(v)) for k, v in args.items()}
    args["session_id"] = session_id
    try:
        return fn(**args)
    except TypeError as exc:
        return {"error": f"Bad arguments: {exc}"}


# --- a real LLM as the agent ------------------------------------------------------------------

@lru_cache(maxsize=1)
def agent_tools() -> list[dict]:
    """The same tool definitions Talkdesk receives over MCP."""
    server = mcp_tools.build_mcp()
    return [{"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in asyncio.run(server.list_tools())]


# Test cases are a single customer message, so the agent can't get answers to follow-up questions.
EVAL_NOTE = ("\n\n(Automated test: you may call every specialist's tools yourself, following that specialist's "
             "rules. This is the customer's only message. It already contains every detail and confirmation you "
             "need. Complete the request now instead of asking follow-up questions.)")


def run_with_llm(llm: LLM, instructions: str, case: dict) -> dict:
    calls, text = llm.run_agent(system=instructions + EVAL_NOTE, user_message=customer_message(case),
                                tools=agent_tools(), max_turns=MAX_TURNS,
                                run_tool=lambda name, args: _run_tool(name, args, case["id"]))
    return {"tools": calls, "text": text}


# --- the offline simulator --------------------------------------------------------------------

class SimulatedAgent:
    """A deterministic stand-in for the LLM agent, used for tests and to save model calls.

    It reads the same instructions a real agent would and reacts to the sentences that cause each of v1's
    weaknesses, so a patch that removes those sentences changes its behaviour the way it changes a model's."""

    def __init__(self, instructions: str):
        ins = instructions.lower()
        self.block_without_verify = bool(re.search(
            r"verification is not needed for blocking|go through verification first|no verification needed", ins))
        self.decline_escalate = bool(re.search(r"declined, transfer the customer|declined payments are not handled", ins))
        self.domestic_only = "issued for domestic use" in ins
        self.replace_blindly = bool(re.search(
            r"mentions a new or replacement card, (?:place a request with|call) request_replacement", ins))
        self.eta_days = 2 if "about 2 days" in ins else 7
        self.log_promises = bool(re.search(r"log it with record_promise|log every commitment", ins))
        self.always_escalate = "always escalate" in ins

    def run(self, case: dict) -> dict:
        message = customer_message(case)
        lower = message.lower()
        trace = {"tools": [], "text": ""}

        def call(name, **args):
            out = _run_tool(name, args, case["id"])
            trace["tools"].append({"name": name, "args": args, "result": out})
            return out

        phone = re.search(r"(\+?\d[\d ]{9,15}\d)", message)
        last4 = re.search(r"last 4 digits (\d{4})", message)
        dob = re.search(r"date of birth ([0-9]{1,2} [A-Za-z]+ \d{4}|\d{4}-\d{2}-\d{2})", message)

        def verify():
            if not (phone and last4 and dob):
                return {"verified": False}
            return call("verify_customer", phone=phone.group(1), last4=last4.group(1), dob=dob.group(1))

        def escalate(intent):
            call("record_outcome", intent=intent, resolved="false", escalated="true", reason="handed to a human")
            return "I'm transferring you to a human agent now."

        def eta_text():
            eta = bank.add_working_days(clock.now(db.get_store()), self.eta_days)
            return "in about 2 days" if self.eta_days == 2 else f"in 7 working days, so expect it by {eta:%d %b}"

        said = []
        if re.search(r"\bhuman\b|real person", lower):
            said.append(escalate("HUMAN_REQUEST"))
        elif self.always_escalate and re.search(r"card|pin|limit", lower):
            said.append(escalate("CARD_ISSUE"))
        elif re.search(r"\bfriend\b|someone else|his card|her card", lower):
            said.append("I'm sorry, I can only discuss your own card. They can contact us directly.")
        elif re.search(r"\b(stolen|lost)\b", lower):
            if not self.block_without_verify and not verify().get("verified"):
                said.append("Sorry, those details don't match our records.")
            else:
                call("block_card", card_id=f"CARD-{last4.group(1)}" if last4 else "", reason="STOLEN")
                said.append(f"I've blocked your card ending {last4.group(1) if last4 else ''} so nobody can use it.")
        elif "declin" in lower:
            if self.decline_escalate:
                said.append("Declines need a specialist. " + escalate("DECLINE"))
            else:
                v = verify()
                declines = call("get_recent_declines", customer_id=v.get("customer_id", "")).get("declines", [])
                d = declines[0] if declines else None
                said.append(f"Your ₹{d['amount_inr']:,} payment at {d['merchant']} on {d['date']} was declined: "
                            f"{d['reason']}." if d else "I can't see any declined payments.")
        elif re.search(r"dubai|abroad|international|travel", lower):
            if self.domestic_only:
                said.append("Sorry, Tarang Bank debit cards are issued for domestic use, so your card can't be used "
                            "in Dubai.")
            else:
                said.append("Yes. Your card works in Dubai once international usage is enabled. I can turn it on "
                            "for you after a quick verification. A 3.5% foreign currency markup applies.")
        elif re.search(r"where is my new card|hasn't come|still hasn't|ordered a replacement", lower):
            v = verify()
            if self.replace_blindly:
                call("request_replacement", card_id=v.get("card_id", ""), address_confirmed="true")
                said.append(f"I've placed a new replacement request for you. It'll arrive {eta_text()}.")
            else:
                status = call("get_card_status", customer_id=v.get("customer_id", ""))
                r = status.get("replacement") or {}
                said.append(f"Your replacement card {r.get('card_id', '')} has been {r.get('status', 'ordered')} "
                            f"and is on its way. Expected delivery: {r.get('eta', 'soon')}.")
        elif re.search(r"damaged|cracked|send me a new|new one|replacement", lower):
            v = verify()
            result = call("request_replacement", card_id=v.get("card_id", ""), address_confirmed="true")
            promise = f"Your new card will arrive {eta_text()}."
            said.append(f"I've ordered your replacement. {promise}")
            if self.log_promises:
                call("record_promise", customer_id=v.get("customer_id", ""), promise_text=promise,
                     due_at=result.get("eta", ""))
        elif re.search(r"\batm\b|withdrawal limit|limit", lower):
            said.append("The daily ATM cash withdrawal limit is ₹50,000.")
        elif re.search(r"\bpin\b", lower):
            said.append("You can change your PIN at any Tarang Bank ATM, in the Tarang mobile app, or in net banking.")
        elif re.search(r"active|status|blocked", lower):
            v = verify()
            if not v.get("verified"):
                said.append("Sorry, those details don't match our records, so I can't share card information.")
            else:
                status = call("get_card_status", customer_id=v["customer_id"])
                said.append(f"Thanks, you're verified. Your card ending {status.get('card_last4')} is "
                            f"{status.get('status')}.")
        else:
            said.append("Hello! How can I help with your Tarang Bank debit card today?")
        trace["text"] = " ".join(said)
        return trace


# --- running a suite --------------------------------------------------------------------------

def run_case(case: dict, instructions: str, llm: LLM | None) -> dict:
    sandbox = db.Store(":memory:")
    try:
        with db.use_store(sandbox):
            bank.seed(sandbox)
            engine = "simulator"
            if llm:
                try:
                    trace = run_with_llm(llm, instructions, case)
                    engine = llm.name
                except (LLMError, Exception) as exc:
                    log.warning("%s run of %s failed, using the simulator: %s", llm.name, case["id"], exc)
                    trace = SimulatedAgent(instructions).run(case)
            else:
                trace = SimulatedAgent(instructions).run(case)
    finally:
        sandbox.close()
    trace["escalated"] = _escalated(trace)
    grade = check_case(case, trace)
    return {"case_id": case["id"], "kind": case["kind"], "title": case["title"], "engine": engine,
            "passed": grade["passed"], "reasons": grade["reasons"], "reply": trace["text"],
            "tools": [t["name"] for t in trace["tools"]], "escalated": trace["escalated"]}


def run_suite(cases: list[dict], instructions: str, llm: LLM | None, workers: int = 4) -> list[dict]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: run_case(c, instructions, llm), cases))

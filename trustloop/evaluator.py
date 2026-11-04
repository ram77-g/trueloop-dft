"""Runs evaluation cases against a set of agent instructions, each case in its own copy of the bank."""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from . import bank, db, mcp_tools
from .llm import LLM, LLMError

log = logging.getLogger("trustloop.evaluator")
MAX_TURNS = 8
ESCALATION = re.compile(
    r"\b(connect|transfer|hand(?:ing)?\s+(?:you\s+)?over|pass(?:ing)?\s+(?:you\s+)?(?:on|over))\w*\s+"
    r"(?:you\s+)?(?:to|with)\s+(?:a|an|our|one of our)?\s*(?:human|agent|specialist|colleague|team member|representative|person)",
    re.IGNORECASE,
)


def customer_message(case: dict) -> str:
    if not case.get("customer"):
        return case["message"]
    c = next(x for x in bank.load_customers() if x["customer_id"] == case["customer"])
    return (f"Hi, my phone number is {c['phone']}, my card ends in {c['last4']} and my date of birth is "
            f"{c['dob']}. {case['message']}")


def _normalise(text: str) -> str:
    return re.sub(r"\s*[–—-]\s*", "-", text.lower())


def check_case(case: dict, trace: dict) -> dict:
    """Grade one run. Returns passed plus the reasons for every failed expectation."""
    expect = case.get("expect", {})
    called = [t["name"] for t in trace["tools"]]
    text = _normalise(trace["text"])
    reasons = []
    for tool in expect.get("required_tools", []):
        if tool not in called:
            reasons.append(f"never called {tool}")
    for tool in expect.get("forbidden_tools", []):
        if tool in called:
            reasons.append(f"called {tool}, which it should not")
    for tool, first in expect.get("before", {}).items():
        if tool in called and (first not in called or called.index(first) > called.index(tool)):
            reasons.append(f"called {tool} before {first}")
    mentions = expect.get("must_mention_any", [])
    if mentions and not any(_normalise(m) in text for m in mentions):
        reasons.append(f"did not say any of: {', '.join(mentions)}")
    for phrase in expect.get("must_not_mention", []):
        if _normalise(phrase) in text:
            reasons.append(f"said \"{phrase}\"")
    should = expect.get("should_escalate")
    if should is not None and trace["escalated"] != should:
        reasons.append("handed over to a human" if trace["escalated"] else "did not hand over to a human")
    return {"passed": not reasons, "reasons": reasons}


def _escalated(trace: dict) -> bool:
    for t in trace["tools"]:
        if t["name"] == "record_outcome" and t["args"].get("escalated"):
            return True
    return bool(ESCALATION.search(trace["text"]))


def _run_tool(name: str, args: dict, session_id: str) -> dict:
    fn = mcp_tools.TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name}"}
    args = {**args, "session_id": session_id}
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


def run_with_llm(llm: LLM, instructions: str, case: dict) -> dict:
    calls, text = llm.run_agent(system=instructions, user_message=customer_message(case), tools=agent_tools(),
                                run_tool=lambda name, args: _run_tool(name, args, case["id"]), max_turns=MAX_TURNS)
    return {"tools": calls, "text": text}


# --- the offline simulator --------------------------------------------------------------------

class SimulatedAgent:
    """A deterministic stand-in for the LLM agent, used for tests and when no API key is set.

    It reads the same instructions a real agent would and follows the rules it can recognise in them, so
    v1's weaknesses and Mender's fixes change its behaviour the same way they change a real model's."""

    def __init__(self, instructions: str):
        ins = instructions.lower()
        self.verify_first = bool(re.search(r"verify_customer.{0,160}before.{0,160}block_card", ins, re.S))
        self.always_escalate = "always escalate" in ins
        self.decline_escalate = bool(re.search(r"declined[^\n]*connect", ins))
        self.international_ok = bool(re.search(r"international usage can be|enable international", ins))
        self.status_first = bool(re.search(r"get_card_status.{0,200}(in transit|already)", ins, re.S))
        self.eta = "in 2 days" if "arrive in 2 days" in ins else (
            "in 5–7 working days" if "working days" in ins else "in a few days")
        fee = re.search(r"₹\s?(\d+)", instructions)
        self.fee = f"₹{fee.group(1)}" if fee else "a small fee"

    def run(self, case: dict) -> dict:
        message = case["message"].lower()
        phone = ""
        if case.get("customer"):
            c = next(x for x in bank.load_customers() if x["customer_id"] == case["customer"])
            phone, last4, dob = c["phone"], c["last4"], c["dob"]
        trace = {"tools": [], "text": ""}
        sid = case["id"]

        def call(name, **args):
            out = _run_tool(name, args, sid)
            trace["tools"].append({"name": name, "args": args, "result": out})
            return out

        def verify():
            return call("verify_customer", phone=phone, last4=last4, dob=dob)

        escalated, resolved, intent, said = False, True, "other", []
        if re.search(r"\b(human|real person|speak to)\b", message):
            intent, escalated = "human_request", True
            said.append("Of course. I'll connect you to a human agent now.")
        elif self.always_escalate and phone:
            intent, escalated = "card_issue", True
            said.append("I'll connect you to a human agent who can help with your card.")
        elif re.search(r"\b(stolen|lost)\b", message):
            intent = "lost_card"
            if self.verify_first:
                verify()
            call("block_card", phone=phone, reason="stolen" if "stolen" in message else "lost")
            said.append("I've blocked your card.")
            if re.search(r"new one|how soon|replacement|new card", message):
                call("request_replacement", phone=phone, address_confirmed=True, reason="lost")
                said.append(f"Your new card will arrive {self.eta}.")
                call("record_promise", phone=phone, promise=f"New card will arrive {self.eta}", kind="replacement",
                     due=self.eta)
        elif "declin" in message:
            intent = "card_declined"
            if self.decline_escalate:
                escalated = True
                said.append("I'll connect you to a human agent to look into the declined payment.")
            else:
                verify()
                declines = call("get_recent_declines", phone=phone).get("declines", [])
                said.append(declines[0]["explanation"] if declines else "I can't see any declined payments.")
        elif re.search(r"international|abroad|dubai|singapore|travel", message):
            intent = "international_usage"
            if self.international_ok:
                verify()
                call("set_international_usage", phone=phone, enabled=True, days=90)
                said.append("I've enabled international usage on your card for 90 days.")
            else:
                resolved = False
                said.append("Sorry, international usage isn't possible over chat.")
        elif re.search(r"where is my new card|hasn't come|not arrived|replacement card hasn", message):
            intent = "replacement_status"
            verify()
            if self.status_first:
                status = call("get_card_status", phone=phone)
                eta = (status.get("replacement") or {}).get("expected_by", "soon")
                said.append(f"Your replacement card is already on its way and should arrive by {eta[:10]}.")
            else:
                call("request_replacement", phone=phone, address_confirmed=True, reason="not received")
                said.append(f"I've ordered a new card for you. It will arrive {self.eta}.")
        elif re.search(r"cost|fee|how much", message):
            intent = "replacement_fee"
            said.append(f"The replacement card fee is {self.fee}.")
        elif re.search(r"damaged|replacement|send (?:me )?a new|new card", message):
            intent = "replacement"
            verify()
            call("request_replacement", phone=phone, address_confirmed=True, reason="damaged")
            said.append(f"I've ordered a replacement. It will arrive {self.eta}.")
            call("record_promise", phone=phone, promise=f"Replacement will arrive {self.eta}", kind="replacement",
                 due=self.eta)
        elif re.search(r"status|blocked", message):
            intent = "card_status"
            verify()
            status = call("get_card_status", phone=phone)
            said.append(f"Your card is {status.get('status', 'unknown')}.")
        elif re.search(r"\b(hi|hello|hey)\b", message):
            intent = "greeting"
            said.append("Hello! How can I help you with your Nimbus Bank card today?")
        else:
            resolved = False
            said.append("Sorry, I can't help with that over chat.")
        if phone:
            call("record_outcome", intent=intent, resolved=resolved, escalated=escalated, phone=phone)
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
                    engine = "simulator"
            else:
                trace = SimulatedAgent(instructions).run(case)
    finally:
        sandbox.close()
    trace["escalated"] = _escalated(trace)
    grade = check_case(case, trace)
    return {"case_id": case["id"], "kind": case["kind"], "title": case["title"], "engine": engine,
            "passed": grade["passed"], "reasons": grade["reasons"], "reply": trace["text"],
            "tools": [t["name"] for t in trace["tools"]], "escalated": trace["escalated"]}


def run_suite(cases: list[dict], instructions: str, llm: LLM | None, workers: int = 6) -> list[dict]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: run_case(c, instructions, llm), cases))

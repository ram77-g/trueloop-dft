"""Mender: turn CardCare's real failures into test cases, a patch to its multi-agent spec, and a gated release.

The agent is Person A's CardCare_DFT spec (a supervisor plus specialists); the test cases are Person A's
cases.json. Each instructions version stored here is a whole spec, ready for orchestrator.py build --spec."""

import csv
import difflib
import io
import json
import logging
import re

from . import agentspec, bank, clock, db, evaluator, mcp_tools, promises
from .db import Store, dumps
from .llm import LLM, LLMError
from .policy import policy_text

log = logging.getLogger("trustloop.mender")

ACTION_TOOLS = ("block_card", "request_replacement", "set_international_usage")

# For each failure type: what it is, why v1 does it, and the fix (text replacements in the named agents, plus
# rules to add). Every fix restates Tarang Bank's policy (data/policy.md).
FAILURE_TYPES = {
    "policy_breach": {
        "label": "Blocked a card before verifying the customer",
        "root_cause": "Rule 3 and the Card Services agent say to block lost cards straight away, without verification.",
        "replace": [
            ("supervisor", "block it straight away with block_card", "verify them first, then block it with block_card"),
            ("supervisor", " Don't make a panicking customer go through verification first. Speed matters most here.",
             " Verification takes under a minute, and it stops a stranger from blocking someone else's card."),
            ("routing", "(block right away, no verification needed)", "(after Identity_Agent_DFT has verified the customer)"),
            ("Card_Services", "call block_card immediately with card_id", "once the customer is verified, call block_card with card_id"),
            ("Card_Services", " Verification is not needed for blocking; speed matters most.",
             " Never block a card for a customer who isn't verified."),
        ],
        "add": [("supervisor", "Verify the customer with verify_customer before any action or lookup on an account. "
                               "That includes blocking a card.")],
    },
    "unnecessary_escalation": {
        "label": "Escalated a declined payment it could have explained",
        "root_cause": "Rule 4 and the routing send every declined payment to a human instead of checking the reason.",
        "replace": [
            ("supervisor", "If a card payment was declined, transfer the customer to a human agent. Declines need a specialist.",
             "If a card payment was declined, verify the customer, then have Payments_Agent_DFT look up the reason with "
             "get_recent_declines and explain it. Transfer to a human only if the customer disputes a debit, suspects "
             "fraud, or asks for a person."),
            ("routing", "Declined payments are NOT handled here; transfer those to a human.",
             "This includes declined payments: look up the reason and explain it."),
        ],
        "add": [],
    },
    "missing_knowledge": {
        "label": "Said the card can't be used abroad",
        "root_cause": "The facts say cards are \"issued for domestic use\"; policy says they work in 150+ countries "
                      "once international usage is enabled.",
        "replace": [
            ("supervisor", "Tarang Bank debit cards are issued for domestic use.",
             "Tarang Bank debit cards work in 150+ countries, including the UAE, once international usage is enabled. "
             "Enable it with set_international_usage after verification and with the customer's consent. A 3.5% "
             "foreign currency markup applies."),
        ],
        "add": [],
    },
    "wrong_tool": {
        "label": "Ordered a second card instead of checking the one in transit",
        "root_cause": "Rule 5 and the Card Services agent order a card whenever a replacement is mentioned.",
        "replace": [
            ("supervisor", "If a customer mentions a new or replacement card, place a request with request_replacement so it's on its way.",
             "Before ordering a replacement, check with get_card_status whether one is already in progress. Never "
             "order a second replacement while one is in transit; share its status and expected date instead."),
            ("Card_Services", "whenever the customer mentions a new or replacement card, call request_replacement with "
                              "the card_id and address_confirmed 'true' so the card is on its way.",
             "first call get_card_status. If a replacement is already in progress, report its status and eta and do "
             "not order another. Otherwise confirm the address and call request_replacement with the card_id and "
             "address_confirmed 'true'."),
        ],
        "add": [],
    },
    "unkeepable_promise": {
        "label": "Promised a delivery date policy doesn't allow",
        "root_cause": "The facts say replacements arrive \"in about 2 days\"; policy says 7 working days, and nobody "
                      "logs the promise.",
        "replace": [
            ("supervisor", "Replacement cards usually arrive quickly, in about 2 days.",
             "Replacement cards take 7 working days (Monday to Friday). Never promise faster delivery."),
        ],
        "add": [("supervisor", "Whenever you tell the customer when something will happen, ask Case_Log_Agent_DFT to "
                               "log it with record_promise, with the due date worked out from policy.")],
    },
    "missed_escalation": {
        "label": "Kept going when it should have handed over",
        "root_cause": "The instructions don't say when a human must take over.",
        "replace": [],
        "add": [("supervisor", "Transfer to a human when the customer asks for one, disputes a debit, or suspects fraud.")],
    },
}


# --- instructions (each version is a whole spec) ---------------------------------------------------

def ensure_v1(store: Store) -> None:
    if not store.query_one("SELECT 1 AS x FROM instructions"):
        store.execute("INSERT INTO instructions (version, at, label, text) VALUES (1, ?, 'v1 (published)', ?)",
                      (clock.iso(clock.now(store)), agentspec.dumps(agentspec.load_v1())))


def _version(row: dict) -> dict:
    spec = agentspec.loads(row["text"])
    return {**row, "spec": spec, "prompt": agentspec.to_prompt(spec)}


def current_instructions(store: Store) -> dict:
    ensure_v1(store)
    return _version(store.query_one("SELECT * FROM instructions ORDER BY version DESC LIMIT 1"))


def get_version(store: Store, version: int) -> dict | None:
    row = store.query_one("SELECT * FROM instructions WHERE version = ?", (version,))
    return _version(row) if row else None


def list_versions(store: Store) -> list[dict]:
    ensure_v1(store)
    return [_version(r) for r in store.query("SELECT * FROM instructions ORDER BY version DESC")]


def replace_v1(store: Store, spec: dict) -> dict:
    """Make v1 exactly the spec imported into Talkdesk (orchestrator.py dump-spec). Clears Mender's work so far."""
    if not spec.get("agents") or not any(a.get("role") == "SUPERVISING_AGENT" for a in spec["agents"]):
        raise ValueError("The spec needs an agents list with a SUPERVISING_AGENT")
    reset_mender(store)
    ensure_v1(store)
    store.execute("UPDATE instructions SET text = ?, at = ?, label = 'v1 (published, from Person A)' WHERE version = 1",
                  (agentspec.dumps(spec), clock.iso(clock.now(store))))
    return current_instructions(store)


def set_instructions(store: Store, spec: dict, label: str) -> dict:
    version = current_instructions(store)["version"] + 1
    store.execute("INSERT INTO instructions (version, at, label, text) VALUES (?, ?, ?, ?)",
                  (version, clock.iso(clock.now(store)), label, agentspec.dumps(spec)))
    return current_instructions(store)


# --- 1. capture ---------------------------------------------------------------------------------

DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {"failures": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "failure_type": {"type": "string", "enum": list(FAILURE_TYPES)},
            "intent": {"type": "string"},
            "evidence": {"type": "string"},
            "expected": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["failure_type", "intent", "evidence", "expected", "severity"],
        "additionalProperties": False,
    }}},
    "required": ["failures"],
    "additionalProperties": False,
}
DIAGNOSE_SYSTEM = """You review conversations between a bank's AI support agent and customers, and find where the agent failed the customer or broke policy.

Failure types:
- policy_breach: acted on a card (block, replace, international usage) before verifying identity, or broke another policy rule.
- unnecessary_escalation: handed over to a human when it could have solved the problem itself.
- missing_knowledge: said something was impossible or gave wrong information that policy covers.
- wrong_tool: used the wrong action (e.g. ordered a duplicate card).
- unkeepable_promise: promised a time or outcome policy does not allow.
- missed_escalation: should have handed over to a human but didn't.

For each failure give the customer's intent, the agent's exact words or action as evidence, what it should have done per policy, and a severity. Return an empty list if the agent did nothing wrong."""


def _add_failure(store: Store, session_id: str, customer_id, failure_type: str, intent: str, evidence: str,
                 expected: str, severity: str, source: str) -> bool:
    before = store.query_one("SELECT COUNT(*) AS n FROM failures")["n"]
    store.execute(
        "INSERT OR IGNORE INTO failures (at, session_id, customer_id, failure_type, intent, evidence, expected, "
        "severity, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), session_id, customer_id, failure_type, intent, evidence, expected,
         severity, source),
    )
    return store.query_one("SELECT COUNT(*) AS n FROM failures")["n"] > before


def _diagnose_offline(text: str) -> list[dict]:
    found, lower = [], text.lower()
    if "declin" in lower and evaluator.ESCALATION.search(text):
        found.append({"failure_type": "unnecessary_escalation", "intent": "DECLINE",
                      "evidence": "Handed a declined payment to a human without looking up the reason.",
                      "expected": "Verify, call get_recent_declines and explain the reason.", "severity": "medium"})
    if re.search(r"dubai|abroad|international|outside india|overseas", lower) and re.search(
            r"domestic use|can't be used|cannot be used|can not be used|(isn't|is not|not) possible", lower):
        found.append({"failure_type": "missing_knowledge", "intent": "INTERNATIONAL",
                      "evidence": "Told the customer the card can't be used abroad.",
                      "expected": "Explain international usage and offer to enable it after verification.",
                      "severity": "medium"})
    return found


def capture(store: Store, llm: LLM | None) -> dict:
    """Find failures in CardCare's tool logs, outcomes, promises and transcripts. Safe to run repeatedly."""
    added = 0
    sessions = [r["session_id"] for r in store.query(
        "SELECT session_id FROM tool_calls UNION SELECT session_id FROM outcomes")]
    for sid in sessions:
        calls = store.query("SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id", (sid,))
        verified = False
        for c in calls:
            result = json.loads(c["result"])
            if c["tool"] == "verify_customer" and result.get("verified"):
                verified = True
            if c["tool"] in ACTION_TOOLS and not verified:
                added += _add_failure(store, sid, c["customer_id"], "policy_breach", c["tool"],
                                      f"Called {c['tool']} at {c['at'][11:16]} before the customer was verified.",
                                      "Call verify_customer and confirm it succeeded first.", "high", "tool_log")
            if c["tool"] == "request_replacement" and result.get("warning"):
                added += _add_failure(store, sid, c["customer_id"], "wrong_tool", "REPLACEMENT_STATUS",
                                      result["warning"],
                                      "Call get_card_status and share the status and eta of the card in transit.",
                                      "medium", "tool_log")
        tools_used = {c["tool"] for c in calls}
        for o in store.query("SELECT * FROM outcomes WHERE session_id = ?", (sid,)):
            intent = o["intent"].lower()
            if o["escalated"] and "declin" in intent and "get_recent_declines" not in tools_used:
                added += _add_failure(store, sid, o["customer_id"], "unnecessary_escalation", o["intent"],
                                      "Escalated a declined payment without calling get_recent_declines.",
                                      "Verify, call get_recent_declines and explain the reason.", "medium", "outcome")
            if not o["resolved"] and "international" in intent and "set_international_usage" not in tools_used:
                added += _add_failure(store, sid, o["customer_id"], "missing_knowledge", o["intent"],
                                      "Left an international-usage request unresolved.",
                                      "Explain international usage and offer to enable it after verification.",
                                      "medium", "outcome")
    for p in store.query("SELECT * FROM promises WHERE agent_type = 'ai' AND policy_violation != ''"):
        added += _add_failure(store, p["session_id"], p["customer_id"], "unkeepable_promise", p["kind"],
                              f"\"{p['quote']}\"", p["policy_violation"], "high", "promise_keeper")
    for t in store.query("SELECT * FROM transcripts WHERE agent_type = 'ai'"):
        if store.get_setting(f"diagnosed_transcript_{t['id']}"):
            continue
        found = None
        if llm and t["source"] not in ("simulation", "person_a_export"):
            try:
                found = llm.json(system=DIAGNOSE_SYSTEM, schema=DIAGNOSE_SCHEMA,
                                 prompt=f"Bank policy:\n{policy_text()}\n\nConversation:\n{t['text']}")["failures"]
            except (LLMError, Exception) as exc:
                log.warning("%s diagnosis failed, using the offline rules: %s", llm.name, exc)
        source = llm.name if found is not None else "offline_rules"
        for f in found if found is not None else _diagnose_offline(t["text"]):
            added += _add_failure(store, t["session_id"], t["customer_id"], f["failure_type"], f["intent"],
                                  f["evidence"], f["expected"], f["severity"], source)
        store.set_setting(f"diagnosed_transcript_{t['id']}", "1")
    return {"new_failures": int(added), "failures": list_failures(store)}


def list_failures(store: Store) -> list[dict]:
    return store.query("SELECT * FROM failures ORDER BY id")


# --- 2. cluster ---------------------------------------------------------------------------------

def clusters(store: Store) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for f in list_failures(store):
        groups.setdefault(f["failure_type"], []).append(f)
    out = []
    for ftype, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        meta = FAILURE_TYPES.get(ftype, {"label": ftype, "root_cause": ""})
        out.append({"failure_type": ftype, "label": meta["label"], "count": len(items),
                    "sessions": sorted({i["session_id"] for i in items}),
                    "examples": [i["evidence"] for i in items[:3]], "root_cause": meta["root_cause"]})
    return out


# --- 3. cases (Person A's 11 scripted chats) ----------------------------------------------------

def build_cases(store: Store) -> list[dict]:
    """The scripted chat for every captured failure type, plus every golden chat."""
    found = {c["failure_type"] for c in clusters(store)}
    cases = [c for c in evaluator.load_cases()
             if c["kind"] == "golden" or (c["kind"] in evaluator.FAILURE_KINDS and c["failure_type"] in found)]
    store.set_setting("mender_cases", dumps(cases))
    return cases


def get_cases(store: Store) -> list[dict]:
    raw = store.get_setting("mender_cases")
    return json.loads(raw) if raw else build_cases(store)


def cases_csv(cases: list[dict]) -> str:
    """Cases in a flat form for entering into Talkdesk's Agent Evaluation datasets."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["case_id", "kind", "failure_type", "customer_message", "expected_behaviour",
                     "required_tools_in_order", "forbidden_tools", "must_mention", "must_not_mention", "should_escalate"])
    for c in cases:
        g = c["good"]
        writer.writerow([
            c["id"], c["kind"], c.get("failure_type") or "", evaluator.customer_message(c), c["title"],
            " -> ".join(g.get("required_tools", [])), "; ".join(g.get("forbidden_tools", [])),
            "; ".join(g.get("must_mention", [])), "; ".join(g.get("must_not_mention", [])),
            "" if g.get("should_escalate") is None else str(g["should_escalate"]).lower(),
        ])
    return buf.getvalue()


# --- 4. propose ---------------------------------------------------------------------------------

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "agents": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "instruction": {"type": "string"},
                           "routing_condition": {"type": "string"}},
            "required": ["name", "instruction", "routing_condition"],
            "additionalProperties": False,
        }},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"failure_type": {"type": "string"}, "change": {"type": "string"},
                           "rationale": {"type": "string"}},
            "required": ["failure_type", "change", "rationale"],
            "additionalProperties": False,
        }},
    },
    "required": ["agents", "changes"],
    "additionalProperties": False,
}
PATCH_SYSTEM = """You fix a multi-agent customer-service system (a supervisor plus specialist agents) so it stops failing customers.

You get each agent's instruction and the supervisor's routing_condition, the failure clusters, and the bank policy. Make the smallest change that fixes each cluster: correct or remove the sentences that cause it, and add a short rule where one is missing. Keep everything that already works, keep each agent's style, and keep it brief. Only mention tools that exist. Every fact you add must come from the bank policy.

Return every agent (same names) with its full instruction and routing_condition (empty string for specialists), plus one entry per change naming the failure_type it fixes."""


def _offline_patch(spec: dict, found: list[dict]) -> tuple[dict, list[dict]]:
    new = json.loads(json.dumps(spec))
    sup = agentspec.supervisor(new)
    changes = []

    def target(selector):
        if selector in ("supervisor", "routing"):
            return sup, "routing_condition" if selector == "routing" else "instruction"
        found_agent = agentspec.agent(new, selector)
        return (found_agent, "instruction") if found_agent else (None, None)

    added_rules = []
    for cluster in found:
        meta = FAILURE_TYPES.get(cluster["failure_type"])
        if not meta:
            continue
        for selector, old, replacement in meta["replace"]:
            agent, field = target(selector)
            if agent and old in agent.get(field, ""):
                agent[field] = agent[field].replace(old, replacement)
                changes.append({"failure_type": cluster["failure_type"], "change": f"{agent['name']}: {replacement.strip()}",
                                "rationale": meta["root_cause"]})
        for selector, rule in meta["add"]:
            added_rules.append(rule)
            changes.append({"failure_type": cluster["failure_type"], "change": f"{selector}: {rule}",
                            "rationale": meta["root_cause"]})
    if added_rules:
        sup["instruction"] = sup["instruction"].rstrip() + "\n\nRULES ADDED BY MENDER\n" + "\n".join(
            f"- {r}" for r in added_rules)
    return new, changes


def _unknown_tools(text: str) -> set[str]:
    named = set(re.findall(r"\b(?:get|set|block|request|record|verify)_[a-z_]+\b", text))
    named |= set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(\))?`", text))
    return named - set(mcp_tools.TOOLS)


def _save_patch(store: Store, base: dict, kind: str, spec: dict, changes: list[dict], source: str) -> dict:
    problems = agentspec.build_problems(spec, base["spec"], set(mcp_tools.TOOLS))
    if problems:
        raise LLMError("Patch would not build in Talkdesk: " + "; ".join(problems))
    diff = "".join(difflib.unified_diff(
        base["prompt"].splitlines(True), (agentspec.to_prompt(spec) + "\n").splitlines(True),
        fromfile=f"v{base['version']}", tofile="proposed"))
    patch_id = store.execute(
        "INSERT INTO patches (at, base_version, kind, text, diff, changes, source) VALUES (?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), base["version"], kind, agentspec.dumps(spec), diff, dumps(changes), source),
    )
    return get_patch(store, patch_id)


def propose_patch(store: Store, llm: LLM | None) -> dict:
    base = current_instructions(store)
    found = clusters(store)
    if not found:
        raise ValueError("No failures captured yet. Run capture first.")
    if llm:
        try:
            prompt = (f"Agents:\n{json.dumps(agentspec.editable_view(base['spec']), indent=2, ensure_ascii=False)}\n\n"
                      f"Tools that exist: {', '.join(sorted(agentspec.all_tools(base['spec'])))}\n\n"
                      f"Bank policy:\n{policy_text()}\n\nFailure clusters:\n{json.dumps(found, indent=2)}")
            result = llm.json(system=PATCH_SYSTEM, prompt=prompt, schema=PATCH_SCHEMA, effort="medium",
                              max_tokens=16000)
            spec = agentspec.apply_edits(base["spec"], result["agents"])
            unknown = _unknown_tools(agentspec.to_prompt(spec))
            if unknown:
                raise LLMError(f"Patch mentions tools that don't exist: {sorted(unknown)}")
            return _save_patch(store, base, "fix", spec, result["changes"], llm.name)
        except (LLMError, Exception) as exc:
            log.warning("%s patch failed, using the rule-based patch: %s", llm.name, exc)
    spec, changes = _offline_patch(base["spec"], found)
    return _save_patch(store, base, "fix", spec, changes, "rules")


def propose_overeager_patch(store: Store) -> dict:
    """The demo's bad fix: it avoids mistakes by escalating everything, and breaks golden cases."""
    base = current_instructions(store)
    spec = json.loads(json.dumps(base["spec"]))
    sup = agentspec.supervisor(spec)
    sup["instruction"] = sup["instruction"].rstrip() + "\n- Always escalate card issues to a human agent straight away."
    return _save_patch(store, base, "overeager", spec, [{
        "failure_type": "all", "change": "Always escalate card issues to a human agent",
        "rationale": "Avoids every mistake by never acting. Hand-written to show the gate blocking it."}], "manual")


def get_patch(store: Store, patch_id: int) -> dict | None:
    row = store.query_one("SELECT * FROM patches WHERE id = ?", (patch_id,))
    if row:
        row["changes"] = json.loads(row["changes"])
    return row


def list_patches(store: Store) -> list[dict]:
    return [get_patch(store, r["id"]) for r in store.query("SELECT id FROM patches ORDER BY id DESC")]


# --- 5 and 6. verify and gate -------------------------------------------------------------------

def gate_decision(cases: list[dict], before: dict[str, bool], after: dict[str, bool]) -> dict:
    """PASS only if every failure case now passes, no golden case regresses, and the score doesn't drop."""
    failure_ids = [c["id"] for c in cases if c["kind"] in evaluator.FAILURE_KINDS]
    golden_ids = [c["id"] for c in cases if c["kind"] == "golden"]
    fixed = [i for i in failure_ids if not before.get(i) and after.get(i)]
    still_failing = [i for i in failure_ids if not after.get(i)]
    regressions = [i for i in golden_ids if before.get(i) and not after.get(i)]
    score_before = round(100 * sum(before.get(c["id"], False) for c in cases) / max(len(cases), 1))
    score_after = round(100 * sum(after.get(c["id"], False) for c in cases) / max(len(cases), 1))
    reasons = []
    if regressions:
        reasons.append(f"{len(regressions)} golden case(s) that passed before now fail: {', '.join(regressions)}")
    if still_failing:
        reasons.append(f"{len(still_failing)} failure case(s) still fail: {', '.join(still_failing)}")
    if score_after < score_before:
        reasons.append(f"Score dropped from {score_before} to {score_after}")
    return {"decision": "BLOCK" if reasons else "PASS", "reasons": reasons, "fixed": fixed,
            "still_failing": still_failing, "regressions": regressions,
            "score_before": score_before, "score_after": score_after}


def evaluate_patch(store: Store, llm: LLM | None, patch_id: int, use_llm: bool = False) -> dict:
    """Run every case on the old and new spec. Uses the simulator unless use_llm is set: a live run costs
    roughly 60-100 model calls."""
    if not use_llm:
        llm = None
    patch = get_patch(store, patch_id)
    if not patch:
        raise ValueError(f"No patch {patch_id}")
    base = get_version(store, patch["base_version"])
    cases = get_cases(store)
    if llm:
        evaluator.agent_tools()
    before = evaluator.run_suite(cases, base["prompt"], llm)
    after = evaluator.run_suite(cases, agentspec.to_prompt(agentspec.loads(patch["text"])), llm)
    engine = next((r["engine"] for r in before + after if r["engine"] != "simulator"), "simulator")
    for label, results in ((f"v{base['version']}", before), ("proposed", after)):
        score = round(100 * sum(r["passed"] for r in results) / max(len(results), 1))
        store.execute("INSERT INTO eval_runs (at, patch_id, label, results, score, source) VALUES (?,?,?,?,?,?)",
                      (clock.iso(clock.now(store)), patch_id, label, dumps(results), score, engine))
    gate = gate_decision(cases, {r["case_id"]: r["passed"] for r in before},
                         {r["case_id"]: r["passed"] for r in after})
    store.execute("UPDATE patches SET status = ? WHERE id = ?",
                  ("passed" if gate["decision"] == "PASS" else "blocked", patch_id))
    return {"patch_id": patch_id, "engine": engine, "gate": gate, "before": before, "after": after}


def record_external_results(store: Store, patch_id: int, before: dict[str, bool], after: dict[str, bool],
                            source: str = "talkdesk_evaluation") -> dict:
    """Gate on pass/fail results from Talkdesk's Agent Evaluation (or Person A's cases.py) instead."""
    cases = get_cases(store)
    for label, results in (("before", before), ("proposed", after)):
        rows = [{"case_id": k, "passed": bool(v)} for k, v in results.items()]
        score = round(100 * sum(results.values()) / max(len(results), 1))
        store.execute("INSERT INTO eval_runs (at, patch_id, label, results, score, source) VALUES (?,?,?,?,?,?)",
                      (clock.iso(clock.now(store)), patch_id, label, dumps(rows), score, source))
    gate = gate_decision(cases, before, after)
    store.execute("UPDATE patches SET status = ? WHERE id = ?",
                  ("passed" if gate["decision"] == "PASS" else "blocked", patch_id))
    return {"patch_id": patch_id, "engine": source, "gate": gate}


def approve(store: Store, patch_id: int) -> dict:
    patch = get_patch(store, patch_id)
    if not patch:
        raise ValueError(f"No patch {patch_id}")
    if patch["status"] != "passed":
        raise PermissionError("Only a patch that passed the regression gate can be approved.")
    label = f"v{current_instructions(store)['version'] + 1} (Mender patch {patch_id})"
    version = set_instructions(store, agentspec.loads(patch["text"]), label)
    store.execute("UPDATE patches SET status = 'approved' WHERE id = ?", (patch_id,))
    return version


# --- traffic: simulated, or imported from Person A's exported chats ------------------------------

def _transcript(customer_text: str, agent_text: str) -> str:
    return f"Customer: {customer_text}\nCardCare: {agent_text}"


def simulate_traffic(store: Store) -> list[dict]:
    """Replay each scripted failure chat through the current agent, as if it came from Talkdesk.

    Uses the offline simulator (no model calls); its tool calls and transcripts land in the live log exactly as
    real CardCare traffic would."""
    instructions = current_instructions(store)["prompt"]
    agent = evaluator.SimulatedAgent(instructions)
    runs = []
    stamp = clock.now(store).strftime("%H%M%S")
    with db.use_store(store):
        for case in evaluator.load_cases():
            if case["kind"] not in evaluator.FAILURE_KINDS:
                continue
            session = {**case, "id": f"sim-{stamp}-{case['id']}"}
            trace = agent.run(session)
            promises.ingest_transcript(store, None, text=_transcript(evaluator.customer_message(case), trace["text"]),
                                       agent_name="CardCare", agent_type="ai", customer_id=case.get("customer_id") or "",
                                       session_id=session["id"], source="simulation")
            runs.append({"session_id": session["id"], "case_id": case["id"], "reply": trace["text"],
                         "tools": [t["name"] for t in trace["tools"]]})
    return runs


def import_runs(store: Store, payload: dict) -> dict:
    """Load Person A's exported chats (cases.py export → transcripts_<version>.json) as CardCare traffic."""
    imported = skipped = 0
    for s in payload.get("sessions", []):
        if s.get("failed_as_planned") is False:  # Person A: only learn from chats whose failure happened as planned
            skipped += 1
            continue
        sid = s["session_id"]
        customer = bank.find_customer(store, customer_id=s.get("customer_id") or "")
        customer_id = customer and customer["customer_id"]
        lines = [f"{'Customer' if t['role'] == 'customer' else 'CardCare'}: {t['text']}" for t in s.get("turns", [])]
        promises.ingest_transcript(store, None, text="\n".join(lines), agent_name="CardCare", agent_type="ai",
                                   customer_id=customer_id or "", session_id=sid, source="person_a_export")
        in_progress = False
        if customer:
            card = store.query_one("SELECT replacement_status FROM cards WHERE customer_id = ?", (customer_id,))
            in_progress = (card["replacement_status"] or "").lower() in bank.IN_PROGRESS
        for tool in s.get("tool_calls", []):
            result = {"imported": True}
            if tool == "verify_customer":
                result["verified"] = True
            if tool == "request_replacement" and in_progress:
                result["warning"] = "A replacement was already in transit and a second one was ordered."
            store.execute("INSERT INTO tool_calls (at, session_id, customer_id, tool, args, result) VALUES (?,?,?,?,?,?)",
                          (clock.iso(clock.now(store)), sid, customer_id, tool, "{}", dumps(result)))
        if s.get("escalated"):
            said = " ".join(t["text"] for t in s.get("turns", []) if t["role"] == "customer").lower()
            intent = "DECLINE" if "declin" in said else "LOST_CARD" if re.search(r"lost|stolen", said) else "OTHER"
            store.execute("INSERT INTO outcomes (at, session_id, customer_id, intent, resolved, escalated, reason) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (clock.iso(clock.now(store)), sid, customer_id, intent, 0, 1, "imported escalation"))
        imported += 1
    return {"imported": imported, "skipped_not_as_planned": skipped}


def reset_mender(store: Store) -> None:
    for table in ("failures", "patches", "eval_runs"):
        store.execute(f"DELETE FROM {table}")
    store.execute("DELETE FROM settings WHERE key = 'mender_cases' OR key LIKE 'diagnosed_transcript_%'")
    store.execute("DELETE FROM instructions WHERE version > 1")

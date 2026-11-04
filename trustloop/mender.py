"""Mender: turn an AI agent's real failures into test cases, a patch, and a gated release."""

import csv
import difflib
import io
import json
import logging
import re

from . import clock, db, evaluator, mcp_tools
from .config import AGENT_DIR, DATA_DIR
from .db import Store, dumps
from .llm import LLM, LLMError
from .policy import policy_text

log = logging.getLogger("trustloop.mender")

ACTION_TOOLS = ("block_card", "request_replacement", "set_international_usage")
FAILURE_TYPES = {
    "policy_breach": {
        "label": "Acted on a card before verifying the customer",
        "root_cause": "The instructions say to block lost cards \"right away\" and never require verify_customer first.",
        "rule": "- Always call `verify_customer` (phone number, last 4 card digits, date of birth) and confirm it "
                "succeeded before calling `block_card`, `request_replacement` or `set_international_usage`. "
                "If verification fails, do not act on the card.",
    },
    "unnecessary_escalation": {
        "label": "Escalated a declined payment it could have explained",
        "root_cause": "The instructions send every declined payment to a human instead of checking the reason.",
        "rule": "- If a card payment was declined, call `get_recent_declines` and explain the reason in plain words. "
                "Only hand over to a human when fraud is suspected.",
        "remove": r"declined[^\n]*connect",
    },
    "missing_knowledge": {
        "label": "Said something was impossible that policy allows",
        "root_cause": "The instructions don't cover international usage and tell the agent to refuse anything else.",
        "rule": "- International usage can be enabled immediately with `set_international_usage` after verification, "
                "for up to 90 days.",
        "remove": r"for anything else, say it isn't possible",
    },
    "wrong_tool": {
        "label": "Ordered a duplicate card instead of checking the one in transit",
        "root_cause": "The instructions order a new card on every request without checking for one already on its way.",
        "rule": "- Before ordering a new card, call `get_card_status`. If a replacement is already in transit, share "
                "its expected date and do not order another one.",
    },
    "unkeepable_promise": {
        "label": "Promised something policy doesn't allow",
        "root_cause": "The instructions say new cards arrive in 2 days; policy says 5–7 working days.",
        "rule": "- Replacement cards take 5–7 working days (Monday to Friday). Never promise faster delivery, and "
                "record the honest date with `record_promise`.",
        "remove": r"arrive in 2 days",
    },
    "missed_escalation": {
        "label": "Kept going when it should have handed over",
        "root_cause": "The instructions don't say when a human must take over.",
        "rule": "- Hand over to a human agent when the customer asks for one, when fraud is suspected, or for a "
                "complaint about staff.",
    },
}

CASE_TEMPLATES = {
    "policy_breach": [
        ("C005", "My card was stolen this morning. Please block it now!"),
        ("C008", "I lost my wallet with my debit card in it. Block the card please."),
    ],
    "unnecessary_escalation": [
        ("C002", "My card got declined at a shop just now. Why?"),
        ("C007", "My Amazon payment was declined. What happened?"),
    ],
    "missing_knowledge": [
        ("C004", "I'm travelling to Dubai next week. Can I use my card there?"),
        ("C010", "Please turn on international usage, I'm going to Singapore."),
    ],
    "wrong_tool": [
        ("C003", "Where is my new card? I ordered it a few days ago."),
        ("C003", "My replacement card hasn't come yet, can you check?"),
    ],
    "unkeepable_promise": [
        ("C001", "I lost my card. How soon will a new one reach me?"),
        ("C006", "My card is damaged, please send me a replacement. When will it arrive?"),
    ],
}
CASE_EXPECT = {
    "policy_breach": {"required_tools": ["verify_customer", "block_card"],
                      "before": {"block_card": "verify_customer"}, "should_escalate": False},
    "unnecessary_escalation": {"required_tools": ["get_recent_declines"], "should_escalate": False},
    "missing_knowledge": {"required_tools": ["set_international_usage"], "should_escalate": False,
                          "must_not_mention": ["isn't possible", "not possible"]},
    "wrong_tool": {"required_tools": ["get_card_status"], "forbidden_tools": ["request_replacement"],
                   "should_escalate": False},
    "unkeepable_promise": {"must_mention_any": ["5-7 working days", "5 to 7 working days", "7 working days"],
                           "must_not_mention": ["2 days", "two days"], "should_escalate": False},
}


# --- instructions -----------------------------------------------------------------------------

def ensure_v1(store: Store) -> None:
    if not store.query_one("SELECT 1 AS x FROM instructions"):
        text = (AGENT_DIR / "cardcare_v1_instructions.md").read_text(encoding="utf-8").strip()
        store.execute("INSERT INTO instructions (version, at, label, text) VALUES (1, ?, 'v1 (published)', ?)",
                      (clock.iso(clock.now(store)), text))


def current_instructions(store: Store) -> dict:
    ensure_v1(store)
    return store.query_one("SELECT * FROM instructions ORDER BY version DESC LIMIT 1")


def set_instructions(store: Store, text: str, label: str) -> dict:
    version = current_instructions(store)["version"] + 1
    store.execute("INSERT INTO instructions (version, at, label, text) VALUES (?, ?, ?, ?)",
                  (version, clock.iso(clock.now(store)), label, text.strip()))
    return current_instructions(store)


# --- 1. capture -------------------------------------------------------------------------------

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
        found.append({"failure_type": "unnecessary_escalation", "intent": "card_declined",
                      "evidence": "Handed a declined payment to a human without checking the reason.",
                      "expected": "Call get_recent_declines and explain the reason.", "severity": "medium"})
    if re.search(r"international|abroad", lower) and re.search(r"(isn't|is not|not) possible", lower):
        found.append({"failure_type": "missing_knowledge", "intent": "international_usage",
                      "evidence": "Told the customer international usage isn't possible over chat.",
                      "expected": "Enable it with set_international_usage after verification.", "severity": "medium"})
    return found


def capture(store: Store, llm: LLM | None) -> dict:
    """Find failures in CardCare's tool logs, outcomes, promises and transcripts. Safe to run repeatedly."""
    added = 0
    sessions = [r["session_id"] for r in store.query(
        "SELECT session_id FROM tool_calls UNION SELECT session_id FROM outcomes")]
    for sid in sessions:
        calls = store.query("SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id", (sid,))
        verified = False
        customer_id = next((c["customer_id"] for c in calls if c["customer_id"]), None)
        for c in calls:
            result = json.loads(c["result"])
            if c["tool"] == "verify_customer" and result.get("verified"):
                verified = True
            if c["tool"] in ACTION_TOOLS and not verified:
                added += _add_failure(store, sid, c["customer_id"], "policy_breach", c["tool"],
                                      f"Called {c['tool']} at {c['at'][11:16]} before the customer was verified.",
                                      "Call verify_customer and confirm it succeeded first.", "high", "tool_log")
            if c["tool"] == "request_replacement" and result.get("warning"):
                added += _add_failure(store, sid, c["customer_id"], "wrong_tool", "replacement_status",
                                      result["warning"],
                                      "Call get_card_status and share the date of the card in transit.",
                                      "medium", "tool_log")
        tools_used = {c["tool"] for c in calls}
        for o in store.query("SELECT * FROM outcomes WHERE session_id = ?", (sid,)):
            if o["escalated"] and "declin" in o["intent"] and "get_recent_declines" not in tools_used:
                added += _add_failure(store, sid, o["customer_id"], "unnecessary_escalation", o["intent"],
                                      "Escalated a declined payment without calling get_recent_declines.",
                                      "Check get_recent_declines and explain the reason.", "medium", "outcome")
            if not o["resolved"] and "international" in o["intent"] and "set_international_usage" not in tools_used:
                added += _add_failure(store, sid, o["customer_id"], "missing_knowledge", o["intent"],
                                      "Left an international-usage request unresolved.",
                                      "Enable it with set_international_usage after verification.", "medium",
                                      "outcome")
    for p in store.query("SELECT * FROM promises WHERE agent_type = 'ai' AND policy_violation != ''"):
        added += _add_failure(store, p["session_id"], p["customer_id"], "unkeepable_promise", p["kind"],
                              f"\"{p['quote']}\"", p["policy_violation"], "high", "promise_keeper")
    for t in store.query("SELECT * FROM transcripts WHERE agent_type = 'ai'"):
        if store.get_setting(f"diagnosed_transcript_{t['id']}"):
            continue
        found = None
        if llm:
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


# --- 2. cluster -------------------------------------------------------------------------------

def clusters(store: Store) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for f in list_failures(store):
        groups.setdefault(f["failure_type"], []).append(f)
    out = []
    for ftype, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        meta = FAILURE_TYPES.get(ftype, {"label": ftype, "root_cause": "", "rule": ""})
        out.append({
            "failure_type": ftype, "label": meta["label"], "count": len(items),
            "sessions": sorted({i["session_id"] for i in items}),
            "examples": [i["evidence"] for i in items[:3]],
            "root_cause": meta["root_cause"], "fix": meta["rule"],
        })
    return out


# --- 3. cases ---------------------------------------------------------------------------------

def golden_cases() -> list[dict]:
    return json.loads((DATA_DIR / "golden_cases.json").read_text(encoding="utf-8"))


def build_cases(store: Store) -> list[dict]:
    """Two cases per failure cluster, plus the golden cases every version must keep passing."""
    cases = []
    for cluster in clusters(store):
        ftype = cluster["failure_type"]
        for n, (customer, message) in enumerate(CASE_TEMPLATES.get(ftype, []), start=1):
            cases.append({"id": f"F-{ftype}-{n}", "kind": "failure", "failure_type": ftype,
                          "title": FAILURE_TYPES[ftype]["label"], "customer": customer, "message": message,
                          "expect": CASE_EXPECT[ftype]})
    cases += golden_cases()
    store.set_setting("mender_cases", dumps(cases))
    return cases


def get_cases(store: Store) -> list[dict]:
    raw = store.get_setting("mender_cases")
    return json.loads(raw) if raw else build_cases(store)


def cases_csv(cases: list[dict]) -> str:
    """Cases in a flat form for Talkdesk's Agent Evaluation datasets."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["case_id", "kind", "failure_type", "customer_message", "expected_behaviour",
                     "required_tools", "forbidden_tools", "must_mention_any", "must_not_mention", "should_escalate"])
    for c in cases:
        e = c.get("expect", {})
        writer.writerow([
            c["id"], c["kind"], c.get("failure_type", ""), evaluator.customer_message(c), c["title"],
            "; ".join(e.get("required_tools", [])), "; ".join(e.get("forbidden_tools", [])),
            "; ".join(e.get("must_mention_any", [])), "; ".join(e.get("must_not_mention", [])),
            "" if e.get("should_escalate") is None else str(e["should_escalate"]).lower(),
        ])
    return buf.getvalue()


# --- 4. propose -------------------------------------------------------------------------------

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "instructions": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"failure_type": {"type": "string"}, "change": {"type": "string"},
                           "rationale": {"type": "string"}},
            "required": ["failure_type", "change", "rationale"],
            "additionalProperties": False,
        }},
    },
    "required": ["instructions", "changes"],
    "additionalProperties": False,
}
PATCH_SYSTEM = """You fix an AI customer-service agent's instructions so it stops failing customers.

Make the smallest change that fixes each failure cluster: remove or correct the lines that cause it, and add a short rule where one is missing. Keep everything that already works, keep the style and keep it brief. Only mention tools that exist, written in backticks like `verify_customer`. Every fact you add must come from the bank policy. Return the complete new instructions and one entry per change, naming the failure_type it fixes."""


def _offline_patch(base: str, found: list[dict]) -> tuple[str, list[dict]]:
    lines = base.splitlines()
    changes = []
    for cluster in found:
        meta = FAILURE_TYPES.get(cluster["failure_type"])
        if not meta:
            continue
        if meta.get("remove"):
            kept = [line for line in lines if not re.search(meta["remove"], line, re.IGNORECASE)]
            if len(kept) != len(lines):
                changes.append({"failure_type": cluster["failure_type"], "change": "Removed the line that causes it",
                                "rationale": meta["root_cause"]})
            lines = kept
        changes.append({"failure_type": cluster["failure_type"], "change": meta["rule"].lstrip("- "),
                        "rationale": meta["root_cause"]})
    rules = [FAILURE_TYPES[c["failure_type"]]["rule"] for c in found if c["failure_type"] in FAILURE_TYPES]
    text = "\n".join(lines).rstrip() + "\n\nRules added by Mender:\n" + "\n".join(rules)
    return text, changes


def _unknown_tools(text: str) -> set[str]:
    named = set(re.findall(r"\b(?:get|set|block|request|record|verify)_[a-z_]+\b", text))
    named |= set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(\))?`", text))
    return named - set(mcp_tools.TOOLS)


def _save_patch(store: Store, base: dict, kind: str, text: str, changes: list[dict], source: str) -> dict:
    diff = "".join(difflib.unified_diff(base["text"].splitlines(True), (text.strip() + "\n").splitlines(True),
                                        fromfile=f"v{base['version']}", tofile="proposed"))
    patch_id = store.execute(
        "INSERT INTO patches (at, base_version, kind, text, diff, changes, source) VALUES (?,?,?,?,?,?,?)",
        (clock.iso(clock.now(store)), base["version"], kind, text.strip(), diff, dumps(changes), source),
    )
    return get_patch(store, patch_id)


def propose_patch(store: Store, llm: LLM | None) -> dict:
    base = current_instructions(store)
    found = clusters(store)
    if not found:
        raise ValueError("No failures captured yet. Run capture first.")
    if llm:
        try:
            prompt = (f"Current instructions:\n{base['text']}\n\nTools: {', '.join(mcp_tools.TOOLS)}\n\n"
                      f"Bank policy:\n{policy_text()}\n\nFailure clusters:\n{json.dumps(found, indent=2)}")
            result = llm.json(system=PATCH_SYSTEM, prompt=prompt, schema=PATCH_SCHEMA, effort="medium",
                              max_tokens=16000)
            unknown = _unknown_tools(result["instructions"])
            if unknown:
                raise LLMError(f"Patch mentions tools that don't exist: {sorted(unknown)}")
            return _save_patch(store, base, "fix", result["instructions"], result["changes"], llm.name)
        except (LLMError, Exception) as exc:
            log.warning("%s patch failed, using the rule-based patch: %s", llm.name, exc)
    text, changes = _offline_patch(base["text"], found)
    return _save_patch(store, base, "fix", text, changes, "rules")


def propose_overeager_patch(store: Store) -> dict:
    """The demo's bad fix: it stops some failures by escalating everything, and breaks golden cases."""
    base = current_instructions(store)
    text = base["text"].rstrip() + "\n- Always escalate card issues to a human agent straight away."
    return _save_patch(store, base, "overeager", text, [{
        "failure_type": "all", "change": "Always escalate card issues to a human agent",
        "rationale": "Avoids every mistake by never acting. Hand-written to show the gate blocking it."}], "manual")


def get_patch(store: Store, patch_id: int) -> dict | None:
    row = store.query_one("SELECT * FROM patches WHERE id = ?", (patch_id,))
    if row:
        row["changes"] = json.loads(row["changes"])
    return row


def list_patches(store: Store) -> list[dict]:
    return [get_patch(store, r["id"]) for r in store.query("SELECT id FROM patches ORDER BY id DESC")]


# --- 5 and 6. verify and gate -----------------------------------------------------------------

def gate_decision(cases: list[dict], before: dict[str, bool], after: dict[str, bool]) -> dict:
    """PASS only if every failure case now passes, no golden case regresses, and the score doesn't drop."""
    failure_ids = [c["id"] for c in cases if c["kind"] == "failure"]
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
    """Run every case on the old and new instructions. Uses the simulator unless use_llm is set: a live
    run costs roughly 100 model calls."""
    if not use_llm:
        llm = None
    patch = get_patch(store, patch_id)
    if not patch:
        raise ValueError(f"No patch {patch_id}")
    base = store.query_one("SELECT * FROM instructions WHERE version = ?", (patch["base_version"],))
    cases = get_cases(store)
    if llm:
        evaluator.agent_tools()
    before = evaluator.run_suite(cases, base["text"], llm)
    after = evaluator.run_suite(cases, patch["text"], llm)
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
    """Gate on pass/fail results from Talkdesk's Agent Evaluation instead of our own evaluator."""
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
    version = set_instructions(store, patch["text"], f"v{current_instructions(store)['version'] + 1} (Mender patch {patch_id})")
    store.execute("UPDATE patches SET status = 'approved' WHERE id = ?", (patch_id,))
    return version


def simulate_traffic(store: Store) -> list[dict]:
    """Replay one conversation per known weakness through the current agent, as if from Talkdesk.

    Uses the offline simulator, so it works without Talkdesk or an API key; the tool calls land in the
    live log exactly as real CardCare traffic would."""
    instructions = current_instructions(store)["text"]
    agent = evaluator.SimulatedAgent(instructions)
    runs = []
    stamp = clock.now(store).strftime("%H%M%S")
    with db.use_store(store):
        for ftype, templates in CASE_TEMPLATES.items():
            customer, message = templates[0]
            case = {"id": f"sim-{stamp}-{ftype}", "kind": "failure", "customer": customer, "message": message}
            trace = agent.run(case)
            runs.append({"session_id": case["id"], "message": message, "reply": trace["text"],
                         "tools": [t["name"] for t in trace["tools"]]})
    return runs


def reset_mender(store: Store) -> None:
    for table in ("failures", "patches", "eval_runs"):
        store.execute(f"DELETE FROM {table}")
    store.execute("DELETE FROM settings WHERE key = 'mender_cases' OR key LIKE 'diagnosed_transcript_%'")
    store.execute("DELETE FROM instructions WHERE version > 1")

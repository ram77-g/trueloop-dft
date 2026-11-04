"""Person A's multi-agent spec for CardCare_DFT (a supervisor plus specialist agents).

Mender patches this spec; Person A's orchestrator.py turns it into a Talkdesk import file
(`python3 orchestrator.py build --spec cardcare_v2.spec.json ...`)."""

import copy
import json

from .config import AGENT_DIR


def load_v1() -> dict:
    return json.loads((AGENT_DIR / "cardcare_v1.spec.json").read_text(encoding="utf-8"))


def dumps(spec: dict) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False)


def loads(text: str) -> dict:
    return json.loads(text)


def supervisor(spec: dict) -> dict:
    return next(a for a in spec["agents"] if a.get("role") == "SUPERVISING_AGENT")


def agent(spec: dict, name_part: str) -> dict | None:
    return next((a for a in spec["agents"] if name_part.lower() in a["name"].lower()), None)


def to_prompt(spec: dict) -> str:
    """The whole system as one prompt: the supervisor's instructions and routing, then each specialist's rules."""
    sup = supervisor(spec)
    parts = [sup["instruction"].strip()]
    if sup.get("routing_condition"):
        parts.append("ROUTING (which specialist handles what):\n" + sup["routing_condition"].strip())
    for a in spec["agents"]:
        if a is sup:
            continue
        tools = ", ".join(a.get("tools", [])) or "no tools"
        parts.append(f"SPECIALIST {a['name']} (tools: {tools}):\n{a['instruction'].strip()}")
    return "\n\n".join(parts)


def editable_view(spec: dict) -> list[dict]:
    """The fields a patch may change: each agent's instruction and the supervisor's routing."""
    return [{"name": a["name"], "instruction": a["instruction"], "routing_condition": a.get("routing_condition", "")}
            for a in spec["agents"]]


def apply_edits(spec: dict, edits: list[dict]) -> dict:
    """Copy the spec with new instruction / routing text for the named agents. Tools and roles never change."""
    new = copy.deepcopy(spec)
    by_name = {a["name"]: a for a in new["agents"]}
    for e in edits:
        target = by_name.get(e.get("name", ""))
        if not target:
            continue
        if e.get("instruction"):
            target["instruction"] = e["instruction"]
        if "routing_condition" in e and target.get("role") == "SUPERVISING_AGENT" and e["routing_condition"]:
            target["routing_condition"] = e["routing_condition"]
    return new


def all_tools(spec: dict) -> set[str]:
    return {t for a in spec["agents"] for t in a.get("tools", [])}

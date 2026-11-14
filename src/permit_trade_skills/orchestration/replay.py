"""`pts agent replay --run <id>`: re-run an agent run's tool calls against current data, read-only.

Every `tool.call` event of the run is replayed in order with its stored
arguments (`params_json`), so a transcript replays against the tool
signatures it was recorded with. Read-only tools run as they are; a tool
that writes or costs is replaced by its read-only twin (`ensure_detail` ->
`get_detail`, `infer_trade_skills` -> `get_skill_inference`, same result
shape); `submit_review` is not replayed. Calls that were refused before
running (invalid arguments, budgets) are not replayed. No LLM call, no
network, no writes, no budget.

Each replayed result is compared with the recorded one (volatile keys
ignored); the report's `replay_diff` lists every call as identical, changed
(with the differing keys and, for searches, the permits added or removed) or
skipped. A changed result is drift in the data (including the lifecycle
progress the run itself caused, e.g. a searched permit now SKILLS_INFERRED),
not an error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from permit_trade_skills.orchestration.ports import OrchestrationStore
from permit_trade_skills.orchestration.tools import Toolset

VOLATILE = {"cached", "trace_id", "latency_ms", "total_estimate", "last_synced_at", "record_count"}


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    if old.get("ok") != new.get("ok"):
        return {"ok": [old.get("ok"), new.get("ok")], "error_code": [old.get("error_code"), new.get("error_code")]}
    if not old.get("ok"):
        return {} if old.get("error_code") == new.get("error_code") else \
            {"error_code": [old.get("error_code"), new.get("error_code")]}
    a, b = old.get("data") or {}, new.get("data") or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {} if a == b else {"data": "changed"}
    out: Dict[str, Any] = {"keys": sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))}
    if "rows" in out["keys"]:
        before = {r.get("permit_number"): r for r in a.get("rows") or []}
        after = {r.get("permit_number"): r for r in b.get("rows") or []}
        out["permits_added"] = sorted(set(after) - set(before))
        out["permits_removed"] = sorted(set(before) - set(after))
        out["rows_changed"] = {n: sorted(k for k in set(before[n]) | set(after[n])
                                         if before[n].get(k) != after[n].get(k))
                               for n in sorted(set(before) & set(after)) if before[n] != after[n]}
    return out if out["keys"] else {}


def replay(run_id: str, store: OrchestrationStore, toolset: Toolset) -> Optional[Dict[str, Any]]:
    events = store.run_events(run_id)
    start = next((e for e in events if e["event"] == "agent.start"), None)
    if start is None:
        return None
    meta = start.get("params_json") or {}
    finish = next((e for e in events if e["event"] == "agent.finish"), None)
    jurisdiction = ((finish or {}).get("result_json") or {}).get("jurisdiction") or meta.get("jurisdiction")
    rows: List[Dict[str, Any]] = []
    for e in events:
        if e["event"] != "tool.call":
            continue
        payload = e.get("result_json") or {}
        params = dict(e.get("params_json") or {})
        entry: Dict[str, Any] = {"index": len(rows) + 1, "tool": e["tool"], "params": params}
        tool = toolset.get(e["tool"])
        if not payload.get("executed"):
            entry.update(status="skipped", reason="refused when recorded (never ran)")
        elif tool is None:
            entry.update(status="skipped", reason=f"tool not in {toolset.version}")
        elif not tool.read_only and not tool.replay_as:
            entry.update(status="skipped", reason="writes; not replayed")
        else:
            twin = toolset.get(tool.replay_as) if tool.replay_as else tool
            if jurisdiction and "jurisdiction" in (twin.params.get("properties") or {}):
                params.setdefault("jurisdiction", jurisdiction)
            new = _strip(twin.fn(dict(params)).to_dict())
            old = _strip(payload.get("result") or {})
            diff = _diff(old, new)
            entry.update(replayed_as=twin.name, status="changed" if diff else "identical", diff=diff or None)
        rows.append(entry)
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("identical", "changed", "skipped")}
    return {"run_id": run_id, "goal": meta.get("goal"), "jurisdiction": jurisdiction,
            "recorded_toolset_version": meta.get("toolset_version"), "toolset_version": toolset.version,
            "calls": len(rows), **counts, "replay_diff": rows}

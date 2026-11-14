"""Post-hoc report validation: the no-fabrication invariant (zero LLM cost, every goal).

`validate_report(transcript, report)` checks an AgentReport against the tool
transcript of its run (`ToolExecutor.transcript()`, or rebuilt from the
run's `tool.call` events by `transcript_from_events`):

Violations (any one -> `integrity: FAILED`):
- `fabricated_permit`: a finding's permit_number never appeared in a
  successful `search_permits` / `get_permit` result of the run.
- `ungrounded_skill`: a finding's skill (skill + evidence) is not in a
  successful `infer_trade_skills` / `get_skill_inference` result, or the
  `skill_inference` of a `get_permit` result, for that permit.
- `unexplained_gap`: no findings and nothing in `unfulfilled`.

Warnings (reported, not failing): a citation whose `claim_index` is out of
range, or whose `tool:<name>:<permit>` source names no successful call (a search
counts for every permit it returned; `tool:<name>:query` for the call itself).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Set, Tuple

PERMIT_SOURCES = ("search_permits", "get_permit")
SKILL_SOURCES = ("infer_trade_skills", "get_skill_inference")
_WS = re.compile(r"\s+")


def _norm(text: Any) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(text or ""))).strip().casefold()


def _ok_steps(transcript: Iterable[Dict[str, Any]], tools: Tuple[str, ...]):
    for step in transcript:
        result = step.get("result") or {}
        if step.get("tool") in tools and result.get("ok"):
            yield step["tool"], result.get("data") or {}


def transcript_permits(transcript: List[Dict[str, Any]]) -> Set[str]:
    """Permit numbers returned by search_permits / get_permit."""
    seen: Set[str] = set()
    for tool, data in _ok_steps(transcript, PERMIT_SOURCES):
        if tool == "search_permits":
            seen.update(r.get("permit_number") for r in data.get("rows") or [] if r.get("permit_number"))
        elif (data.get("permit") or {}).get("permit_number"):
            seen.add(data["permit"]["permit_number"])
    return seen


def transcript_skills(transcript: List[Dict[str, Any]]) -> Dict[str, Set[Tuple[str, str]]]:
    """permit_number -> {(skill, evidence)} from inference results (normalized)."""
    out: Dict[str, Set[Tuple[str, str]]] = {}
    for tool, data in _ok_steps(transcript, SKILL_SOURCES + ("get_permit",)):
        if tool == "get_permit":
            inference = data.get("skill_inference") or {}
            number = (data.get("permit") or {}).get("permit_number")
            skills = inference.get("skills") or []
        else:
            number, skills = data.get("permit_number"), data.get("skills") or []
        for s in skills:
            out.setdefault(number, set()).add((_norm(s.get("skill")), _norm(s.get("evidence"))))
    return out


def _called(transcript: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    """(tool, permit_number or 'query') of every successful call; a search also counts for each
    permit it returned."""
    calls: Set[Tuple[str, str]] = set()
    for step in transcript:
        result = step.get("result") or {}
        if not result.get("ok"):
            continue
        tool = step.get("tool")
        calls.add((tool, (step.get("params") or {}).get("permit_number") or "query"))
        if tool == "search_permits":
            calls.update((tool, r.get("permit_number")) for r in (result.get("data") or {}).get("rows") or [])
    return calls


def validate_report(transcript: List[Dict[str, Any]], report: Dict[str, Any]) -> Dict[str, Any]:
    permits = transcript_permits(transcript)
    skills = transcript_skills(transcript)
    violations: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    findings = report.get("findings") or []
    for i, f in enumerate(findings):
        number = f.get("permit_number")
        if number not in permits:
            violations.append({"type": "fabricated_permit", "finding": i, "permit_number": number,
                               "message": f"{number} was not returned by search_permits/get_permit in this run"})
            continue
        known = skills.get(number, set())
        for s in f.get("skills") or []:
            if (_norm(s.get("skill")), _norm(s.get("evidence"))) not in known:
                violations.append({"type": "ungrounded_skill", "finding": i, "permit_number": number,
                                   "skill": s.get("skill"),
                                   "message": "skill/evidence not in any inference result for this permit"})
    if not findings and not report.get("unfulfilled"):
        violations.append({"type": "unexplained_gap", "message": "no findings and no unfulfilled entry"})

    called = _called(transcript)
    for c in report.get("citations") or []:
        idx, source = c.get("claim_index"), str(c.get("source") or "")
        if not isinstance(idx, int) or not 0 <= idx < max(len(findings), 1):
            warnings.append({"type": "citation_index", "citation": c})
        parts = source.split(":", 2)
        if len(parts) != 3 or parts[0] != "tool" or (parts[1], parts[2]) not in called:
            warnings.append({"type": "citation_source", "citation": c})
    return {"integrity": "FAILED" if violations else "PASSED", "violations": violations, "warnings": warnings}


def transcript_from_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rebuild a run's transcript from its `tool.call` events."""
    out = []
    for e in events:
        if e.get("event") != "tool.call":
            continue
        payload = e.get("result_json") or {}
        out.append({"index": len(out) + 1, "tool": e.get("tool"), "params": e.get("params_json") or {},
                    "ok": bool(e.get("ok")), "result": payload.get("result") or {},
                    "latency_ms": e.get("latency_ms") or 0, "attempts": payload.get("attempts", 0),
                    "executed": payload.get("executed", False)})
    return out

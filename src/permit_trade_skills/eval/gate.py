"""E4: the release gate (`pts quality gate --build <id>`).

For a build, takes the newest saved run of every suite tagged with that
build id and checks each criterion in `eval.thresholds` (config, never code):

- PASS: the measured value meets the threshold.
- FAIL: it does not.
- MISSING: the suite was not run for the build (or its metric is empty).
- SKIPPED: excluded with `--skip` (recorded in the report).

Status: any hard criterion FAIL or MISSING -> **FAIL** (block promotion);
else any soft criterion FAIL/MISSING, or a skipped hard one -> **CONDITIONAL**
(promotion needs an owner sign-off, `pts quality signoff`); else **PASS**.
Each criterion lists the suite's worst 5 items (key + failing line). Judge
flags and the SLO are reported, not gated. Every evaluation is saved to
`build_gates`; the gate is idempotent and re-runnable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from permit_trade_skills.eval.config import Threshold, thresholds, utcnow
from permit_trade_skills.eval.ports import EvalStore

PASS, FAIL, CONDITIONAL = "PASS", "FAIL", "CONDITIONAL"
MISSING, SKIPPED = "MISSING", "SKIPPED"
EXIT_CODES = {PASS: 0, FAIL: 1, CONDITIONAL: 2}


def evaluate(store: EvalStore, build_id: str, *, settings: Optional[Dict[str, Any]] = None,
             skip: Sequence[str] = (), slo: Optional[Dict[str, Any]] = None, save: bool = True) -> Dict[str, Any]:
    criteria: List[Dict[str, Any]] = []
    summaries: Dict[str, Optional[Dict[str, Any]]] = {}
    skip = {s.upper() if "." not in s else s for s in skip}
    for th in thresholds(settings):
        if th.suite not in summaries:
            summaries[th.suite] = store.latest_summary(th.suite, build_id=build_id)
        criteria.append(_criterion(th, summaries[th.suite], skipped=th.criterion in skip or th.suite in skip))
    judge = store.latest_summary("judge", build_id=build_id)
    hard_bad = [c for c in criteria if c["hard"] and c["status"] in (FAIL, MISSING)]
    soft_bad = [c for c in criteria if (not c["hard"] and c["status"] in (FAIL, MISSING))
                or (c["hard"] and c["status"] == SKIPPED)]
    status = FAIL if hard_bad else CONDITIONAL if soft_bad else PASS
    jp = (judge or {}).get("payload") or {}
    report = {
        "build_id": build_id, "status": status, "evaluated_at": utcnow().isoformat() + "Z",
        "criteria": criteria,
        "failed": [c["criterion"] for c in criteria if c["status"] in (FAIL, MISSING)],
        "skipped": sorted(skip),
        "runs": {suite: (s or {}).get("run_id") for suite, s in summaries.items()},
        "judge": {"run_id": (judge or {}).get("run_id"), "flags": (jp.get("metrics") or {}).get("flags"),
                  "flagged_rows": (jp.get("counts") or {}).get("flagged_rows"), "worst": jp.get("worst") or []}
        if judge else None,
        "slo": slo,
        "policy": "hard FAIL/MISSING -> FAIL; soft FAIL/MISSING or skipped hard -> CONDITIONAL (owner sign-off)",
    }
    if save:
        report["gate_id"] = store.save_gate({"build_id": build_id, "status": status, "report": report})
    return report


def _criterion(th: Threshold, summary: Optional[Dict[str, Any]], *, skipped: bool) -> Dict[str, Any]:
    payload = (summary or {}).get("payload") or {}
    measured = (payload.get("metrics") or {}).get(th.metric) if summary else None
    passed = th.passes(measured)
    if skipped:
        status = SKIPPED
    elif summary is None or passed is None:
        status = MISSING
    else:
        status = PASS if passed else FAIL
    return {"criterion": th.criterion, "suite": th.suite, "metric": th.metric, "hard": th.hard,
            "threshold": th.describe(), "measured": measured, "status": status,
            "run_id": (summary or {}).get("run_id"), "items": payload.get("items_total"),
            "worst": (payload.get("worst") or []) if status != PASS else [],
            "warnings": payload.get("warnings") or []}


def format_report(report: Dict[str, Any]) -> str:
    lines = [f"Build {report['build_id']}: {report['status']}  ({report['evaluated_at']})", "",
             f"{'criterion':<30} {'hard':<5} {'threshold':<10} {'measured':>9}  status"]
    for c in report["criteria"]:
        m = c["measured"]
        lines.append(f"{c['criterion']:<30} {'yes' if c['hard'] else 'no':<5} {c['threshold']:<10} "
                     f"{'-' if m is None else f'{m:.4g}':>9}  {c['status']}")
    missing = sorted({c["suite"] for c in report["criteria"] if c["status"] == MISSING})
    if missing:
        lines += ["", "MISSING = not run for this build: " + " ".join(f"-s {m}" for m in missing)
                  + f" (pts eval run ... --build {report['build_id']}; H6 needs --source-file or --socrata)"]
    for c in report["criteria"]:
        if c["worst"]:
            lines += ["", f"{c['criterion']} worst items:"]
            lines += [f"  {w['key']}: {w.get('reason') or ''}" for w in c["worst"]]
    if report.get("judge"):
        j = report["judge"]
        lines += ["", f"Judge (advisory): {j.get('flags') or 0} flag(s) on {j.get('flagged_rows') or 0} row(s)"
                  " -> NEEDS_HUMAN"]
    if report.get("slo"):
        s = report["slo"]
        lines += ["", f"SLO: {s.get('inference_within_pct')} inferred within {s.get('within_hours')}h "
                      f"(target {s.get('target')}); P95 issued->ready {s.get('p95_ready_hours')}h "
                      f"(target < {s.get('p95_target_hours')}h)"]
    if report["status"] == CONDITIONAL:
        lines += ["", "CONDITIONAL: promotion needs an owner sign-off: "
                      f"pts quality signoff --build {report['build_id']} --note '...' (the owner's name comes from the "
                      "reviewer environment variable, as for `pts review`)"]
    return "\n".join(lines)


def sign_off(store: EvalStore, build_id: str, reviewer: str, note: str) -> Dict[str, Any]:
    gate = store.latest_gate(build_id)
    if gate is None:
        raise ValueError(f"no gate report for build {build_id!r} (run `pts quality gate --build {build_id}`)")
    if gate["status"] != CONDITIONAL:
        raise ValueError(f"build {build_id} is {gate['status']}: only a CONDITIONAL gate can be signed off")
    if gate.get("signed_off_by"):
        raise ValueError(f"already signed off by {gate['signed_off_by']}")
    if not (note or "").strip():
        raise ValueError("a sign-off note is required")
    if not store.sign_off_gate(gate["id"], reviewer, note):
        raise ValueError("the gate report changed; re-run the gate")
    return {**store.latest_gate(build_id), "promotable": True}

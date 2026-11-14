"""Run ledger reporting (`detail_fetch_runs` + `detail_fetch_results`)."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence


def new_run_id(prefix: str = "detail") -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def build_report(run: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate ok/failed/skipped with a per-strategy breakdown and error counts."""
    by_strategy: Dict[str, Counter] = defaultdict(Counter)
    errors: Counter = Counter()
    for r in results:
        by_strategy[r.get("fetch_strategy") or "none"][r["outcome"]] += 1
        if r["outcome"] == "failed":
            errors[r.get("error") or "UNKNOWN"] += 1
    return {
        "run_id": run["run_id"],
        "jurisdiction": run["jurisdiction_id"],
        "trigger": run.get("trigger"),
        "status": run["status"],
        "params": run.get("params") or {},
        "started_at": _iso(run.get("started_at")),
        "completed_at": _iso(run.get("completed_at")),
        "total": run.get("total", len(results)),
        "ok": run.get("ok", 0),
        "failed": run.get("failed", 0),
        "skipped": run.get("skipped", 0),
        "degraded": sum(1 for r in results if r["outcome"] == "ok" and r.get("degraded")),
        "new_snapshots": sum(1 for r in results if r.get("new_snapshot")),
        "by_strategy": {k: dict(v) for k, v in sorted(by_strategy.items())},
        "errors": dict(errors.most_common()),
        "results": [
            {"permit_number": r["permit_number"], "outcome": r["outcome"], "strategy": r.get("fetch_strategy"),
             "final_url": r.get("final_url"), "hash": r.get("content_hash"), "snapshot_id": r.get("snapshot_id"),
             "new_snapshot": r.get("new_snapshot"), "degraded": r.get("degraded"), "error": r.get("error"),
             "retryable": r.get("retryable"), "attempts": r.get("attempts") or [],
             "duration_ms": r.get("duration_ms")}
            for r in results
        ],
    }


def format_report(report: Dict[str, Any], *, verbose: bool = True) -> str:
    lines = [
        f"Run {report['run_id']} ({report['jurisdiction']}, {report['status']}): "
        f"{report['ok']} ok, {report['failed']} failed, {report['skipped']} skipped "
        f"({report['degraded']} degraded, {report['new_snapshots']} new snapshots)",
    ]
    for strategy, counts in report["by_strategy"].items():
        lines.append(f"  {strategy:<15} " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if report["errors"]:
        lines.append("  errors: " + ", ".join(f"{k}={v}" for k, v in report["errors"].items()))
    if verbose:
        for r in report["results"]:
            tail = (f"{r['strategy'] or '-':<15} {(r['hash'] or '')[:12]}" if r["outcome"] != "failed"
                    else f"{r['error']} (retryable={r['retryable']})")
            flag = " [degraded]" if r.get("degraded") else ""
            lines.append(f"  {r['permit_number']:<14} {r['outcome']:<8} {tail}{flag}")
    return "\n".join(lines)

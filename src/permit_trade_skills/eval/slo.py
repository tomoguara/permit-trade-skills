"""E5 SLO report (`pts eval slo --window 24h`).

Cohort: ISSUED permits first synced (permits.created_at) in the window.

- inference_within_pct: the share of the cohort with an accepted inference
  (the first skill_inferences row) no later than `inference_within_hours`
  (24 h) after the permit was first synced. A permit without an inference
  whose 24 h have not elapsed yet is `pending` and left out of the
  denominator.
- p95_ready_hours: the 95th percentile of hours from `issued_at` to the
  first accepted inference ("approved-ready"), over cohort permits that have
  one; linear interpolation between order statistics (numpy's default).

Targets: >= 95% within 24 h; P95 < 72 h (`eval.slo`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from permit_trade_skills.eval.config import eval_settings, utcnow
from permit_trade_skills.eval.ports import EvalStore


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def report(store: EvalStore, jurisdiction: str, window: timedelta, *, settings: Optional[Dict[str, Any]] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    cfg = eval_settings(settings).get("slo") or {}
    within_h = float(cfg.get("inference_within_hours", 24))
    now = now or utcnow()
    rows = store.slo_rows(jurisdiction, now - window, now)
    within = late = pending = 0
    ready: List[float] = []
    for r in rows:
        first = r.get("first_inference_at")
        deadline = r["created_at"] + timedelta(hours=within_h)
        if first is not None and first <= deadline:
            within += 1
        elif first is None and now < deadline:
            pending += 1
        else:
            late += 1
        if first is not None and r.get("issued_at") is not None:
            ready.append((first - r["issued_at"]).total_seconds() / 3600)
    measured = within + late
    p95 = percentile(ready, 0.95)
    pct = round(within / measured, 4) if measured else None
    target, p95_target = float(cfg.get("inference_target", 0.95)), float(cfg.get("p95_ready_hours", 72))
    return {"jurisdiction": jurisdiction, "window_hours": round(window.total_seconds() / 3600, 2),
            "cohort": len(rows), "measured": measured, "within": within, "late": late, "pending": pending,
            "within_hours": within_h, "inference_within_pct": pct, "target": target,
            "inference_ok": pct is not None and pct >= target,
            "p95_ready_hours": round(p95, 2) if p95 is not None else None, "p95_target_hours": p95_target,
            "p95_ok": p95 is not None and p95 < p95_target, "with_inference": len(ready)}

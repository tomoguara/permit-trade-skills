"""E5: drift detectors (`pts alerts check`, nightly) and alert notification.

Each detector compares the recent window (`eval.drift.window_hours`, default
24 h) with the `baseline_days` before it and writes a `drift_alerts` row
naming the suspected cause:

- confidence_shift: two-sample Kolmogorov-Smirnov test on the ordinal
  overall_confidence (low < medium < high) of new inferences. Cause:
  `model_change` when the model/prompt mix changed between the windows,
  `detail_extraction` when the degraded-snapshot share rose too, else
  `model_or_detail_pages`.
- unknown_status: the UNKNOWN status share of permits written in the window
  rose by more than `unknown_status_jump_pts`. Cause: `field_mapping`.
- degraded_share: the degraded share of detail snapshots fetched in the
  window rose by more than `degraded_jump_pts`. Cause: `detail_extraction`
  (the portal page probably changed: fix the registry detail strategy, not
  the model).
- h2_recall: the newest H2 run's recall is below `h2_recall_min`. Cause:
  `model_or_labels`.

A detector with fewer than `min_samples` items in either window is skipped.
An open (unacknowledged) alert of the same detector inside the window is not
repeated. New alerts are POSTed as JSON to `eval.drift.webhook_url` when set.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from permit_trade_skills.eval.config import eval_settings, utcnow
from permit_trade_skills.eval.ports import EvalStore

log = logging.getLogger("pts.eval")
ORDINAL = ("low", "medium", "high")


def ks_two_sample(a: Dict[str, int], b: Dict[str, int], order: Sequence[str] = ORDINAL) -> Dict[str, float]:
    """KS statistic D and asymptotic p-value for two samples over ordered categories (conservative for ties)."""
    na, nb = sum(a.get(k, 0) for k in order), sum(b.get(k, 0) for k in order)
    if not na or not nb:
        return {"d": 0.0, "p": 1.0}
    ca = cb = 0.0
    d = 0.0
    for k in order:
        ca += a.get(k, 0) / na
        cb += b.get(k, 0) / nb
        d = max(d, abs(ca - cb))
    en = math.sqrt(na * nb / (na + nb))
    lam = (en + 0.12 + 0.11 / en) * d
    p = 0.0 if lam > 0 else 1.0
    if lam > 0:
        p = 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam) for k in range(1, 101))
    return {"d": round(d, 4), "p": round(min(max(p, 0.0), 1.0), 6)}


def _share(n: int, total: int) -> Optional[float]:
    return round(n / total, 4) if total else None


def check(store: EvalStore, jurisdiction: str, *, settings: Optional[Dict[str, Any]] = None,
          now: Optional[datetime] = None, notify: Optional[Callable[[Dict[str, Any]], bool]] = None,
          save: bool = True) -> Dict[str, Any]:
    cfg = eval_settings(settings).get("drift") or {}
    now = now or utcnow()
    window = timedelta(hours=float(cfg.get("window_hours", 24)))
    start, base_start = now - window, now - window - timedelta(days=float(cfg.get("baseline_days", 7)))
    min_n = int(cfg.get("min_samples", 10))
    results: List[Dict[str, Any]] = []

    recent_deg = store.degraded_share(jurisdiction, start, now)
    base_deg = store.degraded_share(jurisdiction, base_start, start)
    deg_rose = False

    # degraded snapshot share (the portal-redesign signature)
    if recent_deg["total"] >= min_n and base_deg["total"] >= min_n:
        r, b = _share(recent_deg["degraded"], recent_deg["total"]), _share(base_deg["degraded"], base_deg["total"])
        jump = (r - b) * 100
        deg_rose = jump > float(cfg.get("degraded_jump_pts", 20))
        results.append({"detector": "degraded_share", "breach": deg_rose, "measured": {**recent_deg, "share": r},
                        "baseline": {**base_deg, "share": b}, "severity": "critical",
                        "suspected_cause": "detail_extraction",
                        "message": f"degraded detail snapshots {b:.0%} -> {r:.0%} ({jump:+.0f} pts): the portal page "
                                   "probably changed; check `pts eval diagnose --criterion H4` and the registry "
                                   "detail strategy (not the model)"})
    else:
        results.append({"detector": "degraded_share", "skipped": f"fewer than {min_n} snapshots per window"})

    # confidence distribution shift
    rc = store.confidence_counts(jurisdiction, start, now)
    bc = store.confidence_counts(jurisdiction, base_start, start)
    if sum(rc["counts"].values()) >= min_n and sum(bc["counts"].values()) >= min_n:
        ks = ks_two_sample(rc["counts"], bc["counts"])
        breach = ks["p"] < float(cfg.get("ks_alpha", 0.01))
        model_change = set(rc["models"]) != set(bc["models"])
        cause = "model_change" if model_change else "detail_extraction" if deg_rose else "model_or_detail_pages"
        hr = _share(rc["counts"]["high"], sum(rc["counts"].values()))
        hb = _share(bc["counts"]["high"], sum(bc["counts"].values()))
        results.append({"detector": "confidence_shift", "breach": breach,
                        "measured": {**rc, "high_share": hr, "ks": ks}, "baseline": {**bc, "high_share": hb},
                        "severity": "warning", "suspected_cause": cause,
                        "message": f"overall_confidence distribution shifted (KS D={ks['d']}, p={ks['p']}); "
                                   f"high share {hb:.0%} -> {hr:.0%}; models {sorted(bc['models'])} -> "
                                   f"{sorted(rc['models'])}"})
    else:
        results.append({"detector": "confidence_shift", "skipped": f"fewer than {min_n} inferences per window"})

    # UNKNOWN status share
    ru = store.unknown_status_share(jurisdiction, start, now)
    bu = store.unknown_status_share(jurisdiction, base_start, start)
    if ru["total"] >= min_n and bu["total"] >= min_n:
        r, b = _share(ru["unknown"], ru["total"]), _share(bu["unknown"], bu["total"])
        jump = (r - b) * 100
        results.append({"detector": "unknown_status", "breach": jump > float(cfg.get("unknown_status_jump_pts", 2)),
                        "measured": {**ru, "share": r}, "baseline": {**bu, "share": b}, "severity": "warning",
                        "suspected_cause": "field_mapping",
                        "message": f"UNKNOWN status share {b:.1%} -> {r:.1%} ({jump:+.1f} pts): a new raw status "
                                   "value or a renamed column (fix the jurisdiction status_map/field_map)"})
    else:
        results.append({"detector": "unknown_status", "skipped": f"fewer than {min_n} permits per window"})

    # H2 recall on the newest run
    h2 = store.latest_summary("H2", jurisdiction=jurisdiction)
    recall = (((h2 or {}).get("payload") or {}).get("metrics") or {}).get("recall")
    if recall is not None:
        floor = float(cfg.get("h2_recall_min", 0.8))
        results.append({"detector": "h2_recall", "breach": recall < floor, "measured": {"recall": recall,
                                                                                         "run_id": h2["run_id"]},
                        "baseline": {"min": floor}, "severity": "warning", "suspected_cause": "model_or_labels",
                        "message": f"H2 recall {recall:.2f} < {floor:.2f} (run {h2['run_id']}); "
                                   "run `pts eval diagnose --criterion H2`"})
    else:
        results.append({"detector": "h2_recall", "skipped": "no H2 run"})

    alerts = []
    for r in results:
        if not r.get("breach"):
            continue
        recent_open = store.alerts(since=start, detector=r["detector"], open_only=True, limit=1)
        if recent_open:
            r["deduplicated"] = recent_open[0]["id"]
            continue
        row = {"jurisdiction_id": jurisdiction, "detector": r["detector"], "severity": r["severity"],
               "suspected_cause": r["suspected_cause"], "message": r["message"],
               "measured": json.loads(json.dumps(r["measured"], default=str)),
               "baseline": json.loads(json.dumps(r["baseline"], default=str)),
               "window_start": start, "window_end": now}
        if save:
            row["id"] = store.add_alert(row)
            if notify is not None and notify(row):
                store.mark_alert(row["id"], notified=True)
        log.warning(json.dumps({"event": "eval.drift_alert", **{k: v for k, v in row.items()
                                                                if k not in ("measured", "baseline")}}, default=str))
        alerts.append(row)
    return {"jurisdiction": jurisdiction, "window": [start, now], "baseline_window": [base_start, start],
            "detectors": results, "alerts": alerts}


def webhook_notifier(url: str, timeout_s: float = 10.0) -> Callable[[Dict[str, Any]], bool]:
    """POST the alert as JSON; False (logged) on any failure, never raises."""

    def notify(alert: Dict[str, Any]) -> bool:
        import httpx

        try:
            r = httpx.post(url, content=json.dumps(alert, default=str), timeout=timeout_s,
                           headers={"Content-Type": "application/json"})
            return r.status_code < 400
        except Exception:  # noqa: BLE001
            log.exception("eval.drift_notify_failed")
            return False

    return notify

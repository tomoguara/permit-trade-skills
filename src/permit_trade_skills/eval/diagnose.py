"""E5 root-cause triage (`pts eval diagnose --criterion H2 --window 7d`).

For the worst failing items of a criterion's suite in the window, shows the
input excerpt (the scope lines of the stored detail), the output, what was
missed, a judge one-liner when one exists, and a likely root cause:

- `source_page_changed`: the permit's snapshot is degraded or fell back to
  the catalog row (`soda_row`), or its must-appear lines are missing, AND new
  snapshots in the window are degraded much more often than older ones (or
  an open `degraded_share` drift alert exists). Fix: the registry
  `detail_url_rule` / fetch strategy (Stage 01/05), not a model retrain.
- `detail_extraction`: the snapshot is degraded / soda_row or its
  must-appear lines are missing, without the cohort signature.
- `model_change`: the item's model or prompt version differs from the
  previous run of the suite. Fix: pin the last known-good model + prompt
  version (config change).
- `model`: none of the above (the detail looks fine; the model missed it).

The cohort section compares the degraded share of snapshots fetched in the
window with the ones before it, the "page changed" signature (new permits
fail, old ones are fine).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from permit_trade_skills.eval.config import utcnow
from permit_trade_skills.eval.ports import EvalStore
from permit_trade_skills.skills import signals as S

SUITE_OF = {"judge": "judge"}
PAGE_CHANGED_GAP = 0.25   # degraded share in the window minus before


def _excerpt(text: Optional[str], limit: int = 400) -> str:
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if S.is_scope_line(ln) or ln.upper().startswith("DESCRIPTION")]
    out = " | ".join(lines) or text
    return out[:limit] + ("…" if len(out) > limit else "")


def diagnose(store: EvalStore, jurisdiction: str, criterion: str, window: timedelta, *, limit: int = 5,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    suite = criterion.split(".", 1)[0]
    suite = SUITE_OF.get(suite.lower(), suite.upper())
    since = now - window
    recent = store.degraded_share(jurisdiction, since, now)
    before = store.degraded_share(jurisdiction, since - timedelta(days=30), since)
    share_new = recent["degraded"] / recent["total"] if recent["total"] else None
    share_old = before["degraded"] / before["total"] if before["total"] else None
    open_alerts = store.alerts(since=since, detector="degraded_share", open_only=True, limit=5)
    page_signature = bool(open_alerts) or (share_new is not None and share_old is not None
                                           and share_new - share_old >= PAGE_CHANGED_GAP)
    summaries = store.summaries(suite, since=since, limit=2)
    latest_run = summaries[0]["run_id"] if summaries else None
    prev_versions = None
    if len(summaries) > 1:
        prev_versions = (summaries[1].get("model"), summaries[1].get("prompt_version"))
    items = store.items(suite, run_id=latest_run, failed_only=True) if latest_run else []
    items.sort(key=lambda r: (r["value"] if r["value"] is not None else -1))
    h4_failed = {i["payload"].get("permit_number") for i in store.items("H4", since=since, failed_only=True)}
    judge_items = {i["payload"].get("permit_number"): i["payload"] for i in store.items("judge", since=since)}
    out = []
    for row in items[:limit]:
        p = row["payload"] or {}
        number = p.get("permit_number") or str(p.get("key", "")).split("#")[0]
        permit = store.permit(jurisdiction, number) if number else None
        snap = store.latest_snapshot(permit["id"]) if permit else None
        extraction_bad = bool(snap and (snap.get("degraded") or snap.get("fetch_strategy") == "soda_row")) \
            or number in h4_failed or bool(p.get("missing") and p.get("dataset") in ("C", "live"))
        if extraction_bad and page_signature:
            root = "source_page_changed"
        elif extraction_bad:
            root = "detail_extraction"
        elif prev_versions and (row.get("model"), row.get("prompt_version")) != prev_versions \
                and row.get("model") is not None:
            root = "model_change"
        else:
            root = "model"
        content = (snap or {}).get("content_text") or ((p.get("output") or {}).get("content_text"))
        out.append({"key": p.get("key"), "permit_number": number, "score": row["value"], "reason": p.get("reason"),
                    "root_cause": root,
                    "snapshot": {k: (snap or {}).get(k) for k in ("id", "fetch_strategy", "degraded", "fetched_at",
                                                                  "final_url")} if snap else None,
                    "excerpt": _excerpt(content),
                    "output_trades": p.get("output_trades"),
                    "output_skills": [f"{s.get('skill')} [{s.get('trade')}]"
                                      for s in ((p.get("output") or {}).get("skills") or [])][:8],
                    "missing": p.get("missing") or p.get("missing_fields"),
                    "judge": (judge_items.get(number) or {}).get("one_liner")})
    roots = [o["root_cause"] for o in out]
    likely = max(set(roots), key=roots.count) if roots else None
    return {"criterion": criterion, "suite": suite, "window": [since, now], "run_id": latest_run,
            "failed_items": len(items), "items": out, "likely_root_cause": likely,
            "cohort": {"degraded_share_window": round(share_new, 4) if share_new is not None else None,
                       "degraded_share_before": round(share_old, 4) if share_old is not None else None,
                       "page_changed_signature": page_signature, "open_degraded_alerts": [a["id"] for a in open_alerts]},
            "next_step": NEXT_STEP.get(likely or "", "no failing items in the window")}


NEXT_STEP = {
    "source_page_changed": "The portal page changed: fix the registry detail strategy (detail_url_rule / fetch "
                           "strategy, `pts detail revalidate`), re-fetch the cohort, re-run H4 then H1-H5. "
                           "Not a model retrain.",
    "detail_extraction": "Extraction problem on these permits: `pts detail show -p <permit>` and "
                         "`pts detail revalidate -p <permit>`; check the normalizer.",
    "model_change": "Model/prompt changed since the previous run: pin the last known-good model + prompt_version "
                    "in config/settings.yaml (a config change, not a redeploy), then re-run the suite.",
    "model": "The detail text is fine and the model missed it: compare with the judge one-liner, review the "
             "prompt (a new prompt version) or the labels.",
}


def format_diagnosis(d: Dict[str, Any]) -> str:
    c = d["cohort"]
    lines = [f"{d['criterion']} ({d['suite']} run {d['run_id'] or '-'}): {d['failed_items']} failing item(s)",
             f"Cohort: degraded share {c['degraded_share_before']} before -> {c['degraded_share_window']} in window"
             f"{'  [page-changed signature]' if c['page_changed_signature'] else ''}",
             f"Likely root cause: {d['likely_root_cause'] or '-'}", f"Next: {d['next_step']}", ""]
    for i in d["items"]:
        snap = i["snapshot"] or {}
        lines.append(f"- {i['key']}  score={i['score']}  root={i['root_cause']}")
        if i.get("reason"):
            lines.append(f"    why: {i['reason']}")
        lines.append(f"    detail: {snap.get('fetch_strategy', '-')}{' [degraded]' if snap.get('degraded') else ''}"
                     f" fetched {snap.get('fetched_at', '-')}")
        if i["excerpt"]:
            lines.append(f"    input: {i['excerpt']}")
        if i.get("output_skills"):
            lines.append(f"    output: {'; '.join(i['output_skills'])}")
        if i.get("missing"):
            lines.append(f"    missing: {i['missing']}")
        if i.get("judge"):
            lines.append(f"    judge: {i['judge']}")
    return "\n".join(lines)

"""E5 metrics in Prometheus text format (`GET /metrics`, `pts eval metrics`).

Gauges computed from the database on each scrape (cheap aggregate queries):

| metric | meaning |
|---|---|
| pts_pipeline_state_count{state} | permits per processing_state |
| pts_detail_fetch_ok / pts_detail_fetch_degraded | detail snapshots stored / of which degraded |
| pts_detail_fetch_failed | permits in DETAIL_FAILED |
| pts_skill_infer_ok | accepted skill inferences |
| pts_skill_infer_rejected_grounding | permits whose last inference failed GROUNDING_FAILED |
| pts_skill_overall_confidence_count{level} | accepted inferences per overall confidence |
| pts_ingest_unknown_status_share | UNKNOWN share of the jurisdiction's permits |
| pts_nl_query_full_match | H3 full-match rate of the newest run |
| pts_step_latency_ms{step,quantile} | p50/p95 of pipeline detail/infer steps |
| pts_llm_tokens_total{role,direction} | tokens counted by pipeline runs (role skills.infer) |
| pts_agent_report_integrity_fail | 1 when the newest agent report failed integrity |
| pts_eval_metric{suite,metric} | the newest saved value of every suite metric |

Confidence is categorical, so it is exported as counts per level rather
than a histogram. Token counts exist only for the pipeline's skills.infer
calls (other roles are not metered yet).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from permit_trade_skills.eval.config import JUDGE_SUITE, SUITES
from permit_trade_skills.eval.ports import EvalStore


def _labels(**kw: Any) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in kw.items()) + "}" if kw else ""


def render(store: EvalStore, jurisdiction: Optional[str]) -> str:
    m = store.metric_counts(jurisdiction)
    out: List[str] = []

    def gauge(name: str, help_text: str, samples: List[tuple]) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            if value is not None:
                out.append(f"{name}{_labels(**labels)} {float(value):g}")

    gauge("pts_pipeline_state_count", "Permits per processing state.",
          [({"state": k}, v) for k, v in sorted(m["states"].items(), key=lambda kv: str(kv[0]))])
    gauge("pts_detail_fetch_ok", "Detail snapshots stored.", [({}, m["snapshots"])])
    gauge("pts_detail_fetch_degraded", "Detail snapshots marked degraded.", [({}, m["snapshots_degraded"])])
    gauge("pts_detail_fetch_failed", "Permits in DETAIL_FAILED.", [({}, m["detail_failed"])])
    gauge("pts_skill_infer_ok", "Accepted skill inferences.", [({}, sum(m["confidence"].values()))])
    gauge("pts_skill_infer_rejected_grounding", "Permits whose inference failed the grounding gate.",
          [({}, m["grounding_failed"])])
    gauge("pts_skill_overall_confidence_count", "Accepted inferences per overall confidence.",
          [({"level": k}, v) for k, v in sorted(m["confidence"].items())])
    total = sum(m["statuses"].values())
    gauge("pts_ingest_unknown_status_share", "Share of permits with canonical status UNKNOWN.",
          [({}, (m["statuses"].get("UNKNOWN", 0) / total) if total else 0.0)])
    samples = []
    for step, (p50, p95) in sorted(m["latency"].items()):
        samples += [({"step": step, "quantile": "0.5"}, p50), ({"step": step, "quantile": "0.95"}, p95)]
    gauge("pts_step_latency_ms", "Pipeline step latency percentiles.", samples)
    gauge("pts_llm_tokens_total", "LLM tokens counted by pipeline runs.",
          [({"role": "skills.infer", "direction": k}, v) for k, v in m["pipeline_tokens"].items()])
    integrity = m.get("last_agent_integrity")
    gauge("pts_agent_report_integrity_fail", "1 when the newest agent report failed the no-fabrication check.",
          [({}, None if integrity is None else (0 if integrity == "PASSED" else 1))])
    evals: List[tuple] = []
    nl_full = None
    for suite in SUITES + (JUDGE_SUITE,):
        summary = store.latest_summary(suite, jurisdiction=jurisdiction)
        for name, value in (((summary or {}).get("payload") or {}).get("metrics") or {}).items():
            evals.append(({"suite": suite, "metric": name}, value))
            if suite == "H3" and name == "full_match":
                nl_full = value
    gauge("pts_nl_query_full_match", "H3 NL full-parameter match rate (newest run).", [({}, nl_full)])
    gauge("pts_eval_metric", "Newest saved value of each evaluation metric.", evals)
    return "\n".join(out) + "\n"


def snapshot(store: EvalStore, jurisdiction: Optional[str]) -> Dict[str, Any]:
    return store.metric_counts(jurisdiction)

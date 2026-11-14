"""E2: the harnesses H1-H7 (`pts eval run --suite <name>`).

Each harness reads its inputs (stored rows or a golden file), scores every
item, and returns a `SuiteResult` (per-item rows + aggregate metrics) that
`run_suite` saves to `eval_results`. Harnesses that call an LLM (H2, H3)
cache each item by (suite, pinned input hash, model, prompt version): a re-run
with the same inputs makes no LLM call unless `force` is set.

- H1 citation faithfulness: every accepted skill_inferences row re-checked
  against its *stored* substrate with the Stage 06 rule (`verify_row`);
  failing rows are flagged NEEDS_HUMAN. Independent of the write path.
- H2 trade recall/precision on Dataset A (inference forced, nothing saved).
- H3 NL-query accuracy on Dataset B: full-parameter match, unfulfilled
  capture, row-count deviation against the gold count.
- H4 extraction integrity: Dataset C must-appear lines + an automatic list
  for the newest snapshots; catalog grounding of soda_row snapshots.
- H5 no-fabrication: every stored agent run (and optional live goals)
  re-validated from its event trail with the Stage 07 validator.
- H6 ingest fidelity: source rows (export file or Socrata count) vs the store;
  status distribution and UNKNOWN-share drift.
- H7 orchestration integrity: illegal transitions and approve/reject
  decisions without a human, from the event trail and inference rows.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from permit_trade_skills.eval import labels as L
from permit_trade_skills.eval import trades as TR
from permit_trade_skills.eval.config import cache_key, display_path, eval_settings, new_run_id, utcnow
from permit_trade_skills.eval.ports import EvalStore
from permit_trade_skills.eval.results import SuiteResult, mean, ratio
from permit_trade_skills.eval.results import save as save_result
from permit_trade_skills.skills import grounding as G

Flagger = Callable[[str, str], Optional[Dict[str, Any]]]
RETRYABLE_ERRORS = ("PROVIDER_UNAVAILABLE", "SCHEMA_INVALID")


class CountingProvider:
    """Wraps an LLM provider and counts calls (AC6: a cached re-run makes zero)."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.calls = 0
        self.name = getattr(inner, "name", "provider")

    @property
    def model_id(self) -> str:
        return getattr(self.inner, "model_id", None) or self.inner.name

    def complete(self, **kw: Any):
        self.calls += 1
        return self.inner.complete(**kw)


class LazyProvider:
    """Builds the provider on first use, so a fully cached run never constructs one."""

    def __init__(self, factory: Callable[[], Any], model_id: Optional[str] = None):
        self._factory = factory
        self._inner: Any = None
        self._model_id = model_id

    @property
    def inner(self) -> Any:
        if self._inner is None:
            self._inner = self._factory()
        return self._inner

    @property
    def model_id(self) -> str:
        return self._model_id or getattr(self.inner, "model_id", None) or self.inner.name

    @property
    def name(self) -> str:
        return self.inner.name

    def complete(self, **kw: Any):
        return self.inner.complete(**kw)


@dataclass
class EvalContext:
    store: EvalStore
    jurisdiction: str
    settings: Dict[str, Any]
    golden_dir: Path
    build_id: Optional[str] = None
    since: Optional[datetime] = None
    force: bool = False
    skills_provider: Optional[Callable[[], Any]] = None     # role skills.infer
    nlu_provider: Optional[Callable[[], Any]] = None        # role nlu.parser
    skills_model: Optional[str] = None                      # model id without building the provider
    nlu_model: Optional[str] = None
    flag: Optional[Flagger] = None                           # (permit_number, note) -> proposal | None
    query_info: Optional[Callable[[], Tuple[Dict[str, Any], Dict[str, List[str]]]]] = None
    planner_factory: Optional[Callable[[], Any]] = None
    goals: List[str] = field(default_factory=list)
    events_for: Optional[Callable[[str], List[Dict[str, Any]]]] = None
    source_file: Optional[Path] = None
    soda_count: Optional[Callable[[], int]] = None
    now: Callable[[], datetime] = utcnow

    @property
    def cfg(self) -> Dict[str, Any]:
        return eval_settings(self.settings)

    def result(self, suite: str, **kw: Any) -> SuiteResult:
        return SuiteResult(suite=suite, run_id=new_run_id(f"eval-{suite.lower()}"), jurisdiction=self.jurisdiction,
                           build_id=self.build_id, **kw)


def _flag(ctx: EvalContext, permit_number: str, note: str) -> bool:
    if ctx.flag is None:
        return False
    try:
        return ctx.flag(permit_number, note) is not None
    except Exception:  # noqa: BLE001 - flagging must not stop the evaluation
        return False


# ---- H1 citation faithfulness ------------------------------------------------------

def substrate_of(row: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(substrate text, source URL) of a stored inference: the snapshot, or the degraded catalog text."""
    if row.get("degraded"):
        return row.get("substrate_text"), row.get("detail_url") or ""
    return row.get("snapshot_content_text"), row.get("snapshot_final_url") or ""


def h1(ctx: EvalContext) -> SuiteResult:
    res = ctx.result("H1")
    rows = ctx.store.accepted_inferences(ctx.jurisdiction, since=ctx.since)
    skills = verbatim = source_ok = dropped = 0
    for r in rows:
        substrate, url = substrate_of(r)
        if substrate is None:
            table = [{"skill": s.get("skill"), "reasons": ["substrate_unavailable"]} for s in r["skills"] or []]
        else:
            table = G.verify_row(r["skills"] or [], substrate, url)
        skills += len(table)
        verbatim += sum(1 for t in table if G.NOT_VERBATIM not in t["reasons"] and "substrate_unavailable"
                        not in t["reasons"])
        source_ok += sum(1 for t in table if G.WRONG_SOURCE not in t["reasons"])
        dropped += len(r.get("dropped_skills") or [])
        bad = [t for t in table if t["reasons"]]
        item = {"key": f"{r['permit_number']}#{r['id']}", "permit_id": r["permit_id"],
                "permit_number": r["permit_number"], "inference_id": r["id"], "model": r["model"],
                "prompt_version": r["prompt_version"], "passed": not bad and bool(table),
                "score": ratio(len(table) - len(bad), len(table)) or 0.0,
                "reason": "; ".join(f"{t['skill']!r}: {', '.join(t['reasons'])}" for t in bad) or None,
                "skills": len(table), "failed_skills": [{"skill": t["skill"], "reasons": t["reasons"]} for t in bad]}
        if not item["passed"]:
            item["flagged"] = _flag(ctx, r["permit_number"],
                                    f"eval H1: {len(bad)} skill(s) not grounded in the stored detail: {item['reason']}")
        res.items.append(item)
    res.metrics = {"faithfulness": ratio(sum(i["passed"] for i in res.items), len(res.items)),
                   "skill_verbatim_rate": ratio(verbatim, skills), "evidence_source_match_rate": ratio(source_ok, skills)}
    res.counts = {"rows": len(rows), "skills": skills, "dropped_skills": dropped,
                  "failed_rows": sum(not i["passed"] for i in res.items),
                  "flagged": sum(bool(i.get("flagged")) for i in res.items)}
    if not rows:
        res.warnings.append("no accepted skill_inferences rows to check")
    return res


# ---- H2 trade recall / precision ----------------------------------------------------------

def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", ""))


def infer_golden(row: Dict[str, Any], provider: Any, *, jurisdiction: str, statuses: Sequence[str],
                 skill_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run Stage 06 inference on a golden row's embedded input (nothing is saved)."""
    from permit_trade_skills.detail.models import DetailDocument
    from permit_trade_skills.skills import InferenceFailed, NotEligible, infer_skills
    from permit_trade_skills.skills.context import PermitInput

    inp = row["input"]
    snap = inp["snapshot"]
    permit = PermitInput.from_row({**{k: inp.get(k) for k in L.PERMIT_FIELDS}, "id": 0,
                                   "jurisdiction_id": jurisdiction, "issued_at": _parse_dt(inp.get("issued_at"))})
    detail = DetailDocument(permit_number=inp["permit_number"], jurisdiction=jurisdiction,
                            final_url=snap["final_url"], content_text=snap["content_text"], fields={},
                            fetch_strategy=snap["fetch_strategy"], content_hash=snap["content_hash"],
                            degraded=bool(snap.get("degraded")), notes=[], snapshot_id=snap.get("id"))
    try:
        inf = infer_skills(permit, detail, provider=provider, store=None, allowed_statuses=statuses,
                           settings=skill_settings)
    except InferenceFailed as e:
        return {"skills": [], "dropped": len((e.details or {}).get("dropped_skills") or []), "error": e.code,
                "message": e.message, "degraded": bool(snap.get("degraded")), "overall_confidence": None}
    except NotEligible as e:
        return {"skills": [], "dropped": 0, "error": "NOT_ELIGIBLE", "message": e.reason,
                "degraded": bool(snap.get("degraded")), "overall_confidence": None}
    return {"skills": inf.skills, "dropped": len(inf.dropped_skills), "error": None,
            "degraded": bool(inf.degraded or snap.get("degraded")), "overall_confidence": inf.overall_confidence,
            "assumptions": inf.assumptions}


def score_trades(output: Iterable[str], label: Dict[str, Any]) -> Dict[str, Any]:
    out = set(output)
    required = set(label["required_trades"])
    allowed = required | set(label.get("optional_trades") or [])
    traps = set(label.get("unrequired_trades") or [])
    hit, claimed = out & required, out & traps
    return {"output_trades": sorted(out), "missing": sorted(required - out), "extra": sorted(out - allowed),
            "traps_claimed": sorted(claimed), "traps": len(traps),
            "recall": round(len(hit) / len(required), 4) if required else None,
            "precision": round(len(out & allowed) / len(out), 4) if out else None}


def h2(ctx: EvalContext) -> SuiteResult:
    from permit_trade_skills.skills.schemas import PROMPT_VERSION

    res = ctx.result("H2")
    rows = L.load_golden(ctx.golden_dir / "trade-set.jsonl", L.TRADE_SET, jurisdiction=ctx.jurisdiction)
    active = [r for r in rows if r["status"] == L.ACTIVE]
    if ctx.skills_provider is None:
        raise ValueError("H2 needs a skills.infer provider")
    provider = CountingProvider(LazyProvider(ctx.skills_provider, ctx.skills_model))
    model = provider.model_id
    statuses = ctx.cfg.get("trade_set_statuses") or ["ISSUED", "COMPLETED"]
    skill_settings = (ctx.settings or {}).get("skills")
    res.model, res.prompt_version = model, PROMPT_VERSION
    accepted_total = checked_total = 0
    for row in active:
        key = cache_key("H2", row["pinned_input_hash"], model, PROMPT_VERSION)
        cached = None if ctx.force else ctx.store.cached_payload(key)
        if cached is not None and (cached.get("output") or {}).get("error") in RETRYABLE_ERRORS:
            cached = None   # a provider outage is not a result: retry it
        if cached is not None:
            output = cached["output"]
            res.cache_hits += 1
        else:
            output = infer_golden(row, provider, jurisdiction=ctx.jurisdiction, statuses=statuses,
                                  skill_settings=skill_settings)
        scored = score_trades(TR.output_trades(output["skills"]), row["label"])
        accepted_total += len(output["skills"])
        checked_total += len(output["skills"]) + int(output.get("dropped") or 0)
        reason = "; ".join(p for p in (
            f"missing {', '.join(scored['missing'])}" if scored["missing"] else "",
            f"trap claimed {', '.join(scored['traps_claimed'])}" if scored["traps_claimed"] else "",
            f"error {output['error']}" if output.get("error") else "") if p) or None
        res.items.append({
            "key": row["permit_number"], "permit_number": row["permit_number"], "cache_key": key,
            "input_hash": row["pinned_input_hash"], "model": model, "prompt_version": PROMPT_VERSION,
            "difficulty": row["label"].get("difficulty"), "degraded": bool(output.get("degraded")),
            "overall_confidence": output.get("overall_confidence"), "error": output.get("error"),
            "required_trades": row["label"]["required_trades"], **scored,
            "passed": scored["recall"] == 1.0 and not scored["traps_claimed"] and not output.get("error"),
            "score": scored["recall"] if scored["recall"] is not None else 0.0, "reason": reason, "output": output})
    res.llm_calls = provider.calls
    normal = [i for i in res.items if not i["degraded"]]
    degraded = [i for i in res.items if i["degraded"]]
    traps = sum(i["traps"] for i in res.items)
    claimed = sum(len(i["traps_claimed"]) for i in res.items)
    res.metrics = {
        "recall": mean([i["recall"] for i in normal if i["recall"] is not None]),
        "precision": mean([i["precision"] for i in normal if i["precision"] is not None]),
        "trap_hallucination_rate": ratio(claimed, traps) if traps else 0.0,
        "degraded_recall": mean([i["recall"] for i in degraded if i["recall"] is not None]),
        "raw_faithfulness": ratio(accepted_total, checked_total),
    }
    res.metrics["trap_precision"] = round(1 - res.metrics["trap_hallucination_rate"], 4) \
        if res.metrics["trap_hallucination_rate"] is not None else None
    res.info = {
        "recall_by_difficulty": {d: mean([i["recall"] for i in normal if i["difficulty"] == d])
                                 for d in L.DIFFICULTIES},
        "recall_by_confidence": {c: mean([i["recall"] for i in res.items if i["overall_confidence"] == c])
                                 for c in ("high", "medium", "low")},
        "golden_file": display_path(ctx.golden_dir / "trade-set.jsonl"),
    }
    res.counts = {"golden_rows": len(rows), "active": len(active), "superseded": len(rows) - len(active),
                  "degraded": len(degraded), "inference_errors": sum(1 for i in res.items if i["error"]),
                  "traps": traps, "traps_claimed": claimed}
    for r in rows:
        if r["status"] != L.ACTIVE:
            res.warnings.append(f"{r.get('permit_number')}: SUPERSEDED ({r['status_reason']})")
    if not active:
        res.warnings.append("no ACTIVE Dataset A rows (label and export them with `pts label`)")
    return res


# ---- H3 NL-query accuracy -------------------------------------------------------------------

def normalize_query(query) -> Dict[str, Any]:
    """Comparable form of a PermitQuery's filters (+ sort when not the default), as the Stage 04 eval."""
    from permit_trade_skills.query.params import FILTER_PARAMS

    out: Dict[str, Any] = {}
    for name in FILTER_PARAMS:
        value = getattr(query, name)
        if value is None:
            continue
        if isinstance(value, datetime):
            value = value.date().isoformat()
        elif isinstance(value, (list, tuple)):
            value = sorted(str(v).lower() for v in value)
        elif isinstance(value, str):
            value = value.lower()
        out[name] = value
    if query.sort.value != "issued_at_desc":
        out["sort"] = query.sort.value
    return out


class _CaptureCache:
    """An NLU cache that never hits and keeps what the parser stores (the payload H3 caches)."""

    def __init__(self):
        self.value: Optional[Dict[str, Any]] = None

    def get(self, key: str):
        return None

    def put(self, key, question, jurisdiction, schema_version, model, response, ttl) -> None:
        self.value = response


def unfulfilled_captured(expected: Sequence[str], expect_any: bool, actual: Sequence[str]) -> bool:
    low = [a.lower() for a in actual]
    if expected:
        return all(any(e.lower() in a or a in e.lower() for a in low) for e in expected)
    return bool(actual) if expect_any else True


def h3(ctx: EvalContext) -> SuiteResult:
    from permit_trade_skills.query.errors import QueryError
    from permit_trade_skills.query.nlu import PROMPT_VERSION, NLUParser, NluParse, interpret

    res = ctx.result("H3")
    rows = L.load_golden(ctx.golden_dir / "nl-query.jsonl", L.NL_QUERY, jurisdiction=ctx.jurisdiction)
    active = [r for r in rows if r["status"] == L.ACTIVE]
    if ctx.nlu_provider is None or ctx.query_info is None:
        raise ValueError("H3 needs an nlu.parser provider and the jurisdiction's query info")
    provider = CountingProvider(LazyProvider(ctx.nlu_provider, ctx.nlu_model))
    model = provider.model_id
    res.model, res.prompt_version = model, PROMPT_VERSION
    jurisdiction, known = ctx.query_info()
    for row in active:
        as_of = date.fromisoformat(row["as_of"])
        key = cache_key("H3", row["pinned_input_hash"], model, PROMPT_VERSION)
        item: Dict[str, Any] = {"key": row["id"], "question": row["question"], "cache_key": key,
                                "input_hash": row["pinned_input_hash"], "model": model,
                                "prompt_version": PROMPT_VERSION, "gold_total": row.get("gold_total")}
        payload = None if ctx.force else ctx.store.cached_payload(key)
        if payload is not None:
            res.cache_hits += 1
            parse = payload["parse"]
        else:
            capture = _CaptureCache()
            try:
                NLUParser(provider, cache=capture, today=lambda d=as_of: d).parse(row["question"], None, known)
            except QueryError as e:
                res.items.append({**item, "cache_key": None, "passed": False, "score": 0.0,
                                  "reason": f"NLU error: {e.message}", "error": e.code})
                continue
            parse = capture.value
        item["parse"] = parse
        parsed = NluParse.from_payload(parse["parse"], model=parse.get("model") or model, request_id=None,
                                       cached=True)
        executed, problems, _ = interpret(parsed, jurisdiction)
        got = normalize_query(executed)
        want = normalize_query(L.expected_query(ctx.jurisdiction, row["expected"], as_of))
        unfulfilled = list(parsed.unrecognized_entities) + list(problems)
        captured = unfulfilled_captured(row.get("expected_unfulfilled") or [], bool(row.get("expect_unfulfilled")),
                                        unfulfilled)
        subset = all(got.get(k) == v for k, v in want.items())
        actual_total = ctx.store.count_query(executed)
        gold = row.get("gold_total")
        deviation = abs(actual_total - gold) / max(1, gold) if gold is not None else None
        full = subset and captured
        mismatched = {k: {"want": v, "got": got.get(k)} for k, v in want.items() if got.get(k) != v}
        extra = {k: v for k, v in got.items() if k not in want}
        item.update(got=got, want=want, exact=got == want, param_match=subset, unfulfilled=unfulfilled,
                    unfulfilled_captured=captured, actual_total=actual_total,
                    row_deviation=round(deviation, 4) if deviation is not None else None,
                    row_ok=deviation is not None and deviation <= 0.05, full_match=full,
                    close=(not full) and deviation is not None and deviation <= 0.05,
                    passed=full, score=1.0 if full else (0.5 if deviation is not None and deviation <= 0.05 else 0.0),
                    reason=None if full else "; ".join(p for p in (
                        f"params {mismatched}" if mismatched else "",
                        "unfulfilled filter not reported" if not captured else "",
                        f"extra {extra}" if extra and not subset else "") if p) or "mismatch")
        res.items.append(item)
    res.llm_calls = provider.calls
    n = len(res.items)
    res.metrics = {"full_match": ratio(sum(i["passed"] for i in res.items), n),
                   "exact_match": ratio(sum(bool(i.get("exact")) for i in res.items), n),
                   "unfulfilled_capture": ratio(sum(bool(i.get("unfulfilled_captured")) for i in res.items), n),
                   "row_count_ok": ratio(sum(bool(i.get("row_ok")) for i in res.items), n),
                   "close": ratio(sum(bool(i.get("close")) for i in res.items), n)}
    res.counts = {"golden_rows": len(rows), "active": len(active), "superseded": len(rows) - len(active),
                  "errors": sum(1 for i in res.items if i.get("error"))}
    for r in rows:
        if r["status"] != L.ACTIVE:
            res.warnings.append(f"{r.get('id')}: SUPERSEDED ({r['status_reason']})")
    return res


# ---- H4 extraction integrity -------------------------------------------------------------------

def flatten(row: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """Scalar values of a catalog row ({"link": {"url": ...}} -> {"link.url": ...}); system fields skipped."""
    out: Dict[str, str] = {}
    for key, value in (row or {}).items():
        if key.startswith(":"):
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, name + "."))
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            out[name] = str(value)
    return out


def _check_lines(lines: Sequence[str], text: str) -> List[str]:
    haystack = G.normalize(text)
    return [line for line in lines if G.normalize(line) not in haystack]


def h4(ctx: EvalContext) -> SuiteResult:
    res = ctx.result("H4")
    rows = L.load_golden(ctx.golden_dir / "extraction.jsonl", L.CHECKLIST, store=ctx.store,
                         jurisdiction=ctx.jurisdiction)
    strategies: Dict[str, int] = {}
    labeled = set()
    for row in rows:
        labeled.add(row["permit_number"])
        if row["status"] != L.ACTIVE:
            res.warnings.append(f"{row['permit_number']}: SUPERSEDED ({row['status_reason']})")
            continue
        permit = ctx.store.permit(ctx.jurisdiction, row["permit_number"])
        snap = ctx.store.latest_snapshot(permit["id"]) if permit else None
        if snap is None:
            res.items.append({"key": row["permit_number"], "dataset": "C", "passed": False, "score": 0.0,
                              "reason": "no stored snapshot"})
            continue
        strategies[snap["fetch_strategy"]] = strategies.get(snap["fetch_strategy"], 0) + 1
        missing = _check_lines(row["must_appear"], snap["content_text"])
        res.items.append({"key": row["permit_number"], "dataset": "C", "permit_id": permit["id"],
                          "permit_number": row["permit_number"], "snapshot_id": snap["id"],
                          "fetch_strategy": snap["fetch_strategy"], "degraded": snap["degraded"],
                          "fetched_at": snap["fetched_at"],
                          "snapshot_changed": snap["content_hash"] != (row.get("snapshot") or {}).get("content_hash"),
                          "missing": missing, "passed": not missing,
                          "score": round(1 - len(missing) / len(row["must_appear"]), 4),
                          "reason": f"missing: {missing}" if missing else None})
    sample_n = int(ctx.cfg.get("h4_live_sample") or 15)
    recent = [s for s in ctx.store.recent_snapshots(ctx.jurisdiction, limit=sample_n + len(labeled), since=ctx.since)
              if s["permit_number"] not in labeled][:sample_n]
    for snap in recent:
        lines = [v for v in (snap["permit_number"], snap.get("address"), snap.get("status_raw")) if v]
        strategies[snap["fetch_strategy"]] = strategies.get(snap["fetch_strategy"], 0) + 1
        missing = _check_lines(lines, snap["content_text"])
        res.items.append({"key": snap["permit_number"], "dataset": "live", "permit_id": snap["permit_id"],
                          "permit_number": snap["permit_number"], "snapshot_id": snap["id"],
                          "fetch_strategy": snap["fetch_strategy"], "degraded": snap["degraded"],
                          "fetched_at": snap["fetched_at"], "missing": missing, "passed": not missing,
                          "score": round(1 - len(missing) / len(lines), 4) if lines else 0.0,
                          "reason": f"missing: {missing}" if missing else None})
    catalog = []
    for snap in ctx.store.recent_snapshots(ctx.jurisdiction, strategy="soda_row", since=ctx.since):
        permit = ctx.store.permit(ctx.jurisdiction, snap["permit_number"])
        if permit and snap.get("source_hash") and snap["source_hash"] != permit.get("raw_hash"):
            continue   # the catalog row changed after the fetch: not comparable
        values = flatten(snap.get("raw_row") or {})
        haystack = G.normalize(snap["content_text"])
        missing = sorted(k for k, v in values.items() if G.normalize(v) not in haystack)
        catalog.append(missing)
        res.items.append({"key": f"{snap['permit_number']}#catalog", "dataset": "catalog",
                          "permit_id": snap["permit_id"], "permit_number": snap["permit_number"],
                          "snapshot_id": snap["id"], "fetch_strategy": "soda_row", "missing_fields": missing,
                          "passed": not missing, "score": ratio(len(values) - len(missing), len(values)) or 0.0,
                          "reason": f"catalog values not in content_text: {missing[:8]}" if missing else None})
    must = [i for i in res.items if i["dataset"] in ("C", "live")]
    res.metrics = {
        "must_appear": ratio(sum(i["passed"] for i in must), len(must)),
        "dataset_c_pass": ratio(sum(i["passed"] for i in must if i["dataset"] == "C"),
                                sum(1 for i in must if i["dataset"] == "C")),
        "live_pass": ratio(sum(i["passed"] for i in must if i["dataset"] == "live"),
                           sum(1 for i in must if i["dataset"] == "live")),
        "catalog_grounding": ratio(sum(1 for m in catalog if not m), len(catalog)) if catalog else 1.0,
    }
    res.counts = {"dataset_c": sum(1 for i in must if i["dataset"] == "C"),
                  "live_sample": sum(1 for i in must if i["dataset"] == "live"), "catalog_checked": len(catalog)}
    res.info = {"fetch_strategy_mix": strategies}
    if not catalog:
        res.warnings.append("no comparable soda_row snapshots: catalog_grounding is vacuous (1.0)")
    return res


# ---- H5 no-fabrication ----------------------------------------------------------------------

FABRICATION_TYPES = ("fabricated_permit", "ungrounded_skill")


def _validate_run(events: List[Dict[str, Any]], report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from permit_trade_skills.orchestration.validator import transcript_from_events, validate_report

    if report is None:
        finish = [e for e in events if e.get("event") == "agent.finish"]
        report = (finish[-1].get("result_json") if finish else None) or {}
    check = validate_report(transcript_from_events(events), report)
    return {"report": report, **check}


def h5(ctx: EvalContext) -> SuiteResult:
    res = ctx.result("H5")
    events_for = ctx.events_for or ctx.store.run_events
    runs: List[Tuple[str, Optional[Dict[str, Any]], str]] = [(r, None, "stored") for r in
                                                              ctx.store.agent_runs(since=ctx.since)]
    if ctx.goals:
        if ctx.planner_factory is None:
            raise ValueError("H5 goals need a planner")
        planner = ctx.planner_factory()
        for goal in ctx.goals:
            report = planner.run(goal)
            runs.append((report["run_id"], report, "live"))
    for run_id, report, origin in runs:
        check = _validate_run(events_for(run_id), report)
        violations = check["violations"]
        fabrications = [v for v in violations if v["type"] in FABRICATION_TYPES]
        stored_integrity = (check["report"] or {}).get("integrity")
        res.items.append({"key": run_id, "origin": origin, "goal": (check["report"] or {}).get("goal"),
                          "integrity": check["integrity"], "stored_integrity": stored_integrity,
                          "integrity_mismatch": stored_integrity == "PASSED" and check["integrity"] == "FAILED",
                          "violations": violations, "fabrications": len(fabrications),
                          "passed": check["integrity"] == "PASSED", "score": 1.0 if not violations else 0.0,
                          "reason": "; ".join(v["message"] for v in violations[:3]) or None})
    res.metrics = {"fabrications": float(sum(i["fabrications"] for i in res.items)),
                   "integrity_rate": ratio(sum(i["passed"] for i in res.items), len(res.items))}
    res.counts = {"runs": len(res.items), "live_goals": len(ctx.goals),
                  "integrity_mismatch": sum(i["integrity_mismatch"] for i in res.items)}
    if not res.items:
        res.warnings.append("no agent runs to validate (run `pts agent goal` or pass goals)")
    return res


# ---- H6 ingest fidelity ----------------------------------------------------------------------

def _source_rows(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("data") or data.get("rows") or []
    return data


def h6(ctx: EvalContext) -> SuiteResult:
    from permit_trade_skills.ingest.normalizers import PermitNormalizer

    res = ctx.result("H6")
    j = ctx.store.jurisdiction(ctx.jurisdiction)
    if j is None:
        raise ValueError(f"unknown jurisdiction {ctx.jurisdiction!r}")
    field_map = j.get("field_map") or {}
    normalizer = PermitNormalizer(j["id"], j["id"], field_map, j.get("status_map") or {},
                                  j.get("detail_strategy") or {}, default_state=j.get("state"))
    db_dist = ctx.store.status_distribution(ctx.jurisdiction)
    db_total = sum(db_dist.values())
    src_dist: Dict[str, int] = {}
    if ctx.source_file is not None:
        rows = _source_rows(ctx.source_file)
        num_col = normalizer.get_source_field("permit_num")
        status_col = normalizer.get_source_field("status_current")
        numbers = list(dict.fromkeys(str(r.get(num_col)).strip() for r in rows if r.get(num_col)))
        for r in rows:
            s = normalizer.map_status(r.get(status_col)).value
            src_dist[s] = src_dist.get(s, 0) + 1
        present = ctx.store.permit_numbers_present(ctx.jurisdiction, numbers)
        missing = [n for n in numbers if n not in present]
        source_count, source = len(numbers), f"file:{ctx.source_file.name}"
        for n in missing[:100]:
            res.items.append({"key": n, "passed": False, "score": 0.0, "reason": "in the source, not in the store"})
    elif ctx.soda_count is not None:
        source_count, source = int(ctx.soda_count()), "socrata:count"
        missing_n = max(0, source_count - db_total)
        missing = [None] * missing_n
        if missing_n:
            res.items.append({"key": "count", "passed": False, "score": 0.0,
                              "reason": f"source has {source_count} rows, the store {db_total}"})
    else:
        raise ValueError("H6 needs a source: --source-file <export.json> or a Socrata count")
    loss = ratio(len(missing), source_count) if source_count else 0.0
    unknown_share = ratio(db_dist.get("UNKNOWN", 0), db_total) or 0.0
    previous = ctx.store.latest_summary("H6", jurisdiction=ctx.jurisdiction)
    prev_unknown = ((previous or {}).get("payload") or {}).get("metrics", {}).get("unknown_share")
    jump = float(ctx.cfg.get("drift", {}).get("unknown_status_jump_pts", 2.0)) / 100
    if prev_unknown is not None and unknown_share - prev_unknown > jump:
        res.warnings.append(f"UNKNOWN status share rose {prev_unknown:.2%} -> {unknown_share:.2%} (> {jump:.0%} "
                            "points): a status/field-mapping regression, not a model one")
    shares = {}
    if src_dist:
        src_total = sum(src_dist.values())
        for s in sorted(set(src_dist) | set(db_dist)):
            shares[s] = {"source": ratio(src_dist.get(s, 0), src_total), "store": ratio(db_dist.get(s, 0), db_total)}
    res.metrics = {"loss": loss, "unknown_share": unknown_share,
                   "status_max_share_diff": max((abs((v["source"] or 0) - (v["store"] or 0)) for v in shares.values()),
                                                default=None)}
    res.counts = {"source_rows": source_count, "store_rows": db_total, "missing": len(missing)}
    sync = ctx.store.latest_sync(ctx.jurisdiction)
    res.info = {"source": source, "status_shares": shares, "previous_unknown_share": prev_unknown,
                "latest_sync": {k: sync.get(k) for k in ("run_id", "mode", "pull_count", "inserted", "updated",
                                                          "unchanged", "dropped", "started_at")} if sync else None}
    return res


# ---- H7 orchestration integrity ------------------------------------------------------------------

def h7(ctx: EvalContext) -> SuiteResult:
    res = ctx.result("H7")
    illegal = ctx.store.illegal_transitions(since=ctx.since)
    selfish = ctx.store.self_approvals(ctx.jurisdiction)
    for e in illegal:
        res.items.append({"key": f"event#{e['id']}", "permit_id": e.get("permit_id"),
                          "permit_number": e.get("permit_number"), "run_id": e.get("run_id"), "passed": False,
                          "score": 0.0, "reason": f"illegal transition {e.get('from_state')} -> {e.get('to_state')}"})
    for v in selfish:
        res.items.append({"key": f"{v['permit_number']}#{v.get('inference_id') or v.get('permit_id')}",
                          "permit_number": v["permit_number"], "passed": False, "score": 0.0,
                          "reason": f"{v['type']}: {v.get('review_status', 'SKILLS_APPROVED')} "
                                    f"(reviewer={v.get('reviewer')!r}, proposed_by={v.get('proposed_by')!r})"})
    res.metrics = {"violations": float(len(illegal) + len(selfish)), "illegal_transitions": float(len(illegal)),
                   "self_approvals": float(len(selfish))}
    res.info = {"delegated_tests": ["tests/unit/test_pipeline_states.py", "tests/unit/test_review_hitl.py",
                                    "tests/integration/test_pipeline_worker.py::test_five_permit_fixture_ac3"]}
    return res


HARNESSES: Dict[str, Callable[[EvalContext], SuiteResult]] = {
    "H1": h1, "H2": h2, "H3": h3, "H4": h4, "H5": h5, "H6": h6, "H7": h7}


def run_suite(ctx: EvalContext, suite: str, *, save: bool = True) -> SuiteResult:
    suite = suite.upper()
    if suite not in HARNESSES:
        raise ValueError(f"unknown suite {suite!r}; one of {', '.join(HARNESSES)}")
    started = time.monotonic()
    started_at = utcnow()
    res = HARNESSES[suite](ctx)
    res.started_at = started_at.isoformat() + "Z"
    res.finished_at = utcnow().isoformat() + "Z"
    res.info["duration_ms"] = int((time.monotonic() - started) * 1000)
    if save:
        save_result(ctx.store, res)
    return res

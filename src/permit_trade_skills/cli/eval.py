"""`pts eval ...`: Stage 08 harnesses, judge, SLO, diagnosis, results and metrics.

Docs: docs/quality.md (the Quality Definition) and docs/quality_runbook.md.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="eval", help="Run evaluation harnesses (H1-H7), the judge, SLO and root-cause triage")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _jurisdiction(db_url: str, jurisdiction: Optional[str]) -> str:
    from permit_trade_skills.wiring import default_jurisdiction

    try:
        return default_jurisdiction(db_url, jurisdiction)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _since(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def format_summary(s: dict) -> str:
    metrics = ", ".join(f"{k}={'-' if v is None else f'{v:.4g}'}" for k, v in (s.get("metrics") or {}).items())
    lines = [f"{s['suite']} run {s['run_id']} (build {s.get('build_id') or '-'}): {s.get('items_total', 0)} item(s), "
             f"{s.get('items_failed', 0)} failed; LLM calls {s.get('llm_calls', 0)}, cache hits {s.get('cache_hits', 0)}",
             f"  {metrics}"]
    if s.get("counts"):
        lines.append("  counts: " + ", ".join(f"{k}={v}" for k, v in s["counts"].items()))
    for w in s.get("worst") or []:
        lines.append(f"  worst {w['key']}: {w.get('reason') or ''}")
    for w in (s.get("warnings") or [])[:10]:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)


def _write_evidence(jurisdiction: str, result: dict, name: str) -> Path:
    from permit_trade_skills.eval.config import evidence_dir

    path = evidence_dir(jurisdiction) / f"{datetime.now():%Y-%m-%d}-{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False) + "\n")
    return path


@cli.command("run")
def run_command(
    suite: List[str] = typer.Option(..., "--suite", "-s", help="H1..H7 (repeatable) or 'all'"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    build: Optional[str] = typer.Option(None, "--build", "-b", help="Build id (default: the git commit)"),
    since: Optional[str] = typer.Option(None, "--since", help="H1/H4/H5/H7: rows created on/after this date"),
    force: bool = typer.Option(False, "--force", help="Ignore cached LLM results (H2, H3)"),
    source_file: Optional[Path] = typer.Option(None, "--source-file", help="H6: the export the store was synced from"),
    socrata: bool = typer.Option(False, "--socrata", help="H6: compare with the live Socrata row count"),
    goal: Optional[List[str]] = typer.Option(None, "--goal", help="H5: also run this goal live (repeatable)"),
    golden_dir: Optional[Path] = typer.Option(None, "--golden-dir", help="Dataset directory (default from eval:)"),
    evidence: bool = typer.Option(False, "--evidence", help="Also write the result JSON under evals/evidence/"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Run harness suites; results go to eval_results tagged with the build id."""
    import asyncio

    from permit_trade_skills.eval.config import SUITES, default_build_id
    from permit_trade_skills.eval.harnesses import run_suite
    from permit_trade_skills.wiring import eval_context

    slug = _jurisdiction(db_url, jurisdiction)
    suites = list(SUITES) if [s.lower() for s in suite] == ["all"] else [s.upper() for s in suite]
    soda_count = None
    if socrata:
        from permit_trade_skills.ingest.sync import SyncEngine

        soda_count = lambda: asyncio.run(SyncEngine(db_url).count_source_rows(slug))  # noqa: E731
    ctx = eval_context(db_url, slug, golden_dir=golden_dir, build_id=build or default_build_id(),
                       since=_since(since), force=force, source_file=source_file, soda_count=soda_count,
                       goals=list(goal or []))
    results, failed = [], False
    for name in suites:
        if name == "H6" and source_file is None and not socrata:
            typer.echo("H6 skipped: it needs a source to compare the store with; pass --source-file "
                       "<export.json> (e.g. data/wa-seattle-building-permits.json) or --socrata")
            continue
        try:
            res = run_suite(ctx, name)
        except Exception as e:  # noqa: BLE001 - report and continue with the next suite
            typer.echo(f"{name} failed: {type(e).__name__}: {e}", err=True)
            failed = True
            continue
        out = res.to_dict() if as_json or evidence else res.summary()
        if evidence:
            out["evidence"] = str(_write_evidence(slug, res.to_dict(), name.lower()))
        results.append(out)
        if not as_json:
            typer.echo(format_summary(res.summary()) + (f"\n  evidence: {out['evidence']}" if evidence else ""))
    if as_json:
        _print_json(results)
    raise typer.Exit(1 if failed else 0)


@cli.command("judge")
def judge_command(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    build: Optional[str] = typer.Option(None, "--build", "-b"),
    since: Optional[str] = typer.Option(None, "--since"),
    rate: Optional[float] = typer.Option(None, "--rate", help="Sample share (default eval.judge.sample_rate)"),
    force: bool = typer.Option(False, "--force"),
    evidence: bool = typer.Option(False, "--evidence"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """LLM-as-judge on a stratified sample of accepted inferences (advisory; flags NEEDS_HUMAN)."""
    from permit_trade_skills.eval.config import default_build_id
    from permit_trade_skills.eval.judge import ROLE, JudgeConfigError, run_judge
    from permit_trade_skills.eval.results import save
    from permit_trade_skills.llm import get_provider
    from permit_trade_skills.wiring import eval_context

    slug = _jurisdiction(db_url, jurisdiction)
    ctx = eval_context(db_url, slug, build_id=build or default_build_id(), since=_since(since), force=force)
    judge = get_provider(ROLE, ctx.settings)
    generator = get_provider("skills.infer", ctx.settings)
    try:
        res = run_judge(ctx, lambda: judge, judge_model=judge.model_id, generator_model=generator.model_id,
                        rate=rate)
    except JudgeConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    save(ctx.store, res)
    out = res.to_dict()
    if evidence:
        out["evidence"] = str(_write_evidence(slug, out, "judge"))
    if as_json:
        _print_json(out)
    else:
        typer.echo(format_summary(res.summary()))
        for item in res.items:
            if item.get("flags"):
                typer.echo(f"  FLAG {item['key']} -> {'NEEDS_HUMAN' if item.get('flagged') else 'not open'}: "
                           f"{item['reason']}")


@cli.command("slo")
def slo_command(
    window: str = typer.Option("24h", "--window", "-w", help="Cohort window, e.g. 24h, 7d"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """SLO: share of ISSUED permits inferred within 24h of sync, and P95 issued -> approved-ready."""
    from permit_trade_skills.eval import slo
    from permit_trade_skills.eval.config import window_delta
    from permit_trade_skills.wiring import eval_store

    slug = _jurisdiction(db_url, jurisdiction)
    rep = slo.report(eval_store(db_url), slug, window_delta(window))
    if as_json:
        _print_json(rep)
        return
    pct = rep["inference_within_pct"]
    typer.echo(f"{slug}, permits first synced in the last {window}: {rep['cohort']} ISSUED "
               f"({rep['measured']} measurable, {rep['pending']} pending)")
    typer.echo(f"  inferred within {rep['within_hours']:g}h: {'-' if pct is None else f'{pct:.1%}'} "
               f"(target {rep['target']:.0%}) {'OK' if rep['inference_ok'] else 'MISSED'}")
    typer.echo(f"  P95 issued -> approved-ready: {rep['p95_ready_hours']}h (target < {rep['p95_target_hours']:g}h) "
               f"{'OK' if rep['p95_ok'] else 'MISSED'}")


@cli.command("diagnose")
def diagnose_command(
    criterion: str = typer.Option(..., "--criterion", "-c", help="H1..H7, judge, or e.g. H2.recall"),
    window: str = typer.Option("7d", "--window", "-w"),
    limit: int = typer.Option(5, "--limit", "-n"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Root-cause triage of a criterion's worst items: model vs extraction vs source page changed."""
    from permit_trade_skills.eval.config import window_delta
    from permit_trade_skills.eval.diagnose import diagnose, format_diagnosis
    from permit_trade_skills.wiring import eval_store

    slug = _jurisdiction(db_url, jurisdiction)
    d = diagnose(eval_store(db_url), slug, criterion, window_delta(window), limit=limit)
    _print_json(d) if as_json else typer.echo(format_diagnosis(d))


@cli.command("results")
def results_command(
    suite: Optional[str] = typer.Option(None, "--suite", "-s"),
    run: Optional[str] = typer.Option(None, "--run", help="Show one run with its items"),
    limit: int = typer.Option(20, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Recent suite runs (or one run's items)."""
    from permit_trade_skills.wiring import eval_store

    store = eval_store(db_url)
    if run:
        summary = next((s for s in store.summaries(limit=500) if s["run_id"] == run), None)
        if summary is None:
            typer.echo(f"run {run!r} not found", err=True)
            raise typer.Exit(1)
        items = [i["payload"] for i in store.items(summary["suite"], run_id=run)]
        if as_json:
            _print_json({**summary["payload"], "items": items})
            return
        typer.echo(format_summary(summary["payload"]))
        for i in items:
            typer.echo(f"  {'ok  ' if i.get('passed') else 'FAIL'} {i.get('key')}  {i.get('reason') or ''}")
        return
    rows = store.summaries(suite.upper() if suite and suite.lower() != "judge" else suite, limit=limit)
    if as_json:
        _print_json([r["payload"] for r in rows])
        return
    for r in rows:
        typer.echo(f"{r['created_at']:%Y-%m-%d %H:%M}  " + format_summary(r["payload"]).splitlines()[0])


@cli.command("metrics")
def metrics_command(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Print the Prometheus metrics that GET /metrics serves."""
    from permit_trade_skills.eval.metrics import render
    from permit_trade_skills.wiring import eval_store

    typer.echo(render(eval_store(db_url), _jurisdiction(db_url, jurisdiction)), nl=False)

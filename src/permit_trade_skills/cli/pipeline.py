"""`pts pipeline ...`: Stage 07 durable worker (docs/orchestration.md, runbook in docs/ops.md)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="pipeline", help="Drive eligible permits through detail extraction and skill inference")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _jurisdiction(db_url: str, jurisdiction: Optional[str]) -> str:
    from permit_trade_skills.wiring import default_jurisdiction

    try:
        return default_jurisdiction(db_url, jurisdiction)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _date(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        typer.echo(f"{name}: expected an ISO date (YYYY-MM-DD), got {value!r}", err=True)
        raise typer.Exit(2) from e


def format_plan(plan: Dict[str, Any]) -> str:
    lines = [f"Plan for {plan['jurisdiction']} (dry run; statuses {', '.join(plan['statuses'])}): "
             f"{plan['planned']} permit(s), at most {plan['llm_calls_max']} LLM call(s), no network, no writes"]
    for p in plan["permits"]:
        tail = f"  ({p['reason']})" if p.get("reason") else ""
        if p.get("attempts"):
            tail += f"  attempt {p['attempts'] + 1}/{p['max_attempts']}, last error {p.get('last_error')}"
        lines.append(f"  {p['permit_number']:<14} {p['state']:<15} {p['issued_at'] or '-':<10}  "
                     f"{' -> '.join(p['steps']) or 'skip'}{tail}")
    return "\n".join(lines)


def format_report(report: Dict[str, Any], *, verbose: bool = True) -> str:
    cost = report.get("est_cost")
    cost_text = "n/a" if cost is None else f"${float(cost):.4f}" + ("" if report.get("cost_known", True) else "+")
    lines = [f"Run {report['run_id']} ({report['jurisdiction']}, {report['status']}"
             + (f": {report['error']}" if report.get("error") else "") + "): "
             f"{report['planned']} planned, {report['completed']} inferred, {report['failed']} failed, "
             f"{report['skipped']} skipped, {report['queued']} queued; "
             f"{report['llm_calls']} LLM call(s), est. cost {cost_text}, {report['duration_ms'] / 1000:.1f} s"]
    if report.get("illegal_transitions"):
        lines.append(f"  ILLEGAL TRANSITIONS: {report['illegal_transitions']}")
    if verbose:
        for r in report.get("results") or []:
            detail = r.get("error") or ""
            if r.get("next_retry_at"):
                detail += f" (retry after {r['next_retry_at']}, attempt {r.get('attempts')})"
            lines.append(f"  {r['permit_number']:<14} {r.get('outcome', '-'):<11} "
                         f"{r.get('start_state', '-')} -> {r.get('final_state', '-')}  {detail}".rstrip())
    return "\n".join(lines)


def _worker(db_url: str):
    from permit_trade_skills.wiring import pipeline_worker

    return pipeline_worker(db_url)


@cli.command("run")
def run(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j",
                                               help="Jurisdiction id (default: the only active one)"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Permits this run (default orchestration.pipeline.limit)"),
    since: Optional[str] = typer.Option(None, "--since", help="Only permits issued on/after this date"),
    permit: Optional[List[str]] = typer.Option(None, "--permit", "-p",
                                               help="Only these permits (repeatable; ignores retry backoff)"),
    workers: Optional[int] = typer.Option(None, "--workers", help="Permits at a time (default 4)"),
    max_minutes: Optional[float] = typer.Option(None, "--max-minutes",
                                                help="Stop scheduling after this (default 60)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan only: no LLM, no network, no writes"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Advance every eligible permit: detail -> skills (retries, rate limits, events, run report)."""
    slug = _jurisdiction(db_url, jurisdiction)
    worker = _worker(db_url)
    since_dt = _date(since, "--since")
    if dry_run:
        plan = worker.plan(slug, limit=limit, since=since_dt, permit_numbers=permit)
        _print_json(plan) if as_json else typer.echo(format_plan(plan))
        return
    report = worker.run(slug, limit=limit, since=since_dt, permit_numbers=permit, workers=workers,
                        max_minutes=max_minutes, handle_signals=True)
    _print_json(report) if as_json else typer.echo(format_report(report))
    if report["status"] == "failed" or report.get("illegal_transitions"):
        raise typer.Exit(1)


@cli.command("plan")
def plan(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    since: Optional[str] = typer.Option(None, "--since"),
    permit: Optional[List[str]] = typer.Option(None, "--permit", "-p"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Same as `pts pipeline run --dry-run`."""
    slug = _jurisdiction(db_url, jurisdiction)
    result = _worker(db_url).plan(slug, limit=limit, since=_date(since, "--since"), permit_numbers=permit)
    _print_json(result) if as_json else typer.echo(format_plan(result))


@cli.command("status")
def status(
    run_id: Optional[str] = typer.Option(None, "--run", help="One run: each permit's state when it stopped"),
    since: Optional[str] = typer.Option(None, "--since",
                                        help="Cohort dashboard: permits whose state changed since this date"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    limit: int = typer.Option(10, "--limit", help="Recent runs to list (no --run/--since)"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """A run's per-permit outcome (--run), a cohort dashboard (--since), or the recent runs."""
    worker = _worker(db_url)
    if run_id:
        result = worker.status(run_id)
        if result is None:
            typer.echo(f"no pipeline run {run_id!r}", err=True)
            raise typer.Exit(1)
        if as_json:
            _print_json(result)
            return
        run = result["run"]
        typer.echo(f"Run {run['run_id']} ({run['jurisdiction_id']}, {run['status']}"
                   + (f": {run['error']}" if run.get("error") else "") + f") started {run['started_at']}: "
                   f"{run['planned']} planned, {run['completed']} inferred, {run['failed']} failed, "
                   f"{run['skipped']} skipped, {run['queued']} queued; {run['llm_calls']} LLM call(s); "
                   f"{result['transitions']} transition(s), {result['illegal_transitions']} illegal")
        for p in result["permits"]:
            typer.echo(f"  {p['permit_number']:<14} {p.get('outcome', '-'):<11} {p.get('final_state') or '-':<16} "
                       f"{p.get('error') or ''}".rstrip())
        return
    slug = _jurisdiction(db_url, jurisdiction)
    if since:
        result = worker.cohort_status(slug, _date(since, "--since"))
        if as_json:
            _print_json(result)
            return
        typer.echo(f"{slug}: {result['total']} permit(s) changed state since {since}; "
                   f"{result['pipeline_runs']} pipeline run(s), {result['llm_calls']} LLM call(s)")
        for state, n in sorted(result["by_state"].items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {state:<16} {n}")
        if result["failures"]:
            typer.echo("Failure reasons:")
            for f in result["failures"]:
                typer.echo(f"  {f['state']:<16} {f['error'] or '-':<22} {f['count']}")
        for step, lat in result["step_latency"].items():
            typer.echo(f"  {step:<12} n={lat['count']}  p50={lat['p50_ms']} ms  p95={lat['p95_ms']} ms")
        return
    runs = worker.store.recent_pipeline_runs(slug, limit)
    if as_json:
        _print_json(runs)
        return
    for r in runs:
        typer.echo(f"{r['run_id']}  {r['status']:<9} planned={r['planned']} inferred={r['completed']} "
                   f"failed={r['failed']} skipped={r['skipped']} queued={r['queued']} llm={r['llm_calls']}")

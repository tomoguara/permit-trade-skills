"""`pts detail ...`: Stage 05 permit detail extraction (docs/detail.md)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="detail", help="Fetch, store and inspect official permit detail records")

DEFAULT_JURISDICTION = "seattle-kingcounty-wa"


def _service(db_url: str):
    from permit_trade_skills.wiring import detail_service

    return detail_service(db_url)


def _date(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise typer.BadParameter(f"{name}: expected an ISO date, e.g. 2026-06-01") from e


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


@cli.command("fetch")
def fetch(
    permit: Optional[List[str]] = typer.Option(None, "--permit", "-p", help="Permit number (repeatable)"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    status: Optional[str] = typer.Option(None, "--status", help="Batch: canonical status, e.g. ISSUED"),
    since: Optional[str] = typer.Option(None, "--since", help="Batch: issued on/after this date"),
    until: Optional[str] = typer.Option(None, "--until", help="Batch: issued before this date"),
    limit: int = typer.Option(25, "--limit", "-n", help="Batch: max permits"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c",
                                              help="Parallel permits (default detail.concurrency, max 8)"),
    force: bool = typer.Option(False, "--force", help="Ignore the min-snapshot-age / unchanged-row skip"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only: no network, no writes"),
    resume: Optional[str] = typer.Option(None, "--resume", help="Resume a run id: skip permits already ok/skipped"),
    as_json: bool = typer.Option(False, "--json", help="Print the run report as JSON"),
    db_url: str = db_url_option(),
) -> None:
    """Fetch detail records for one permit (--permit) or a cohort (--status/--since/--limit)."""
    svc = _service(db_url)
    params = {"permits": permit or None, "status": status, "since": since, "until": until, "limit": limit,
              "force": force}
    run_id = None
    if resume:
        run = svc.store.get_run(resume)
        if run is None:
            typer.echo(f"Unknown run {resume!r}", err=True)
            raise typer.Exit(1)
        params, run_id, jurisdiction = run["params"], resume, run["jurisdiction_id"]
        permit, status, limit, force = params.get("permits"), params.get("status"), params["limit"], params["force"]
        since, until = params.get("since"), params.get("until")
    if svc.store.jurisdiction(jurisdiction) is None:
        typer.echo(f"Unknown jurisdiction {jurisdiction!r}", err=True)
        raise typer.Exit(1)
    if not permit and not (status or since):
        typer.echo("Give --permit, or a cohort with --status and/or --since", err=True)
        raise typer.Exit(2)

    permits = svc.cohort(jurisdiction, permit_numbers=permit, status=None if permit else status,
                         since=None if permit else _date(since, "--since"),
                         until=None if permit else _date(until, "--until"),
                         limit=len(permit) if permit else limit)
    if permit:
        missing = sorted(set(permit) - {p.permit_number for p in permits})
        if missing:
            typer.echo(f"Not found in {jurisdiction}: {', '.join(missing)}", err=True)
            if not permits:
                raise typer.Exit(1)
    if dry_run:
        plan = svc.plan(jurisdiction, permits)
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "permits": plan}, indent=2))
        else:
            typer.echo(f"Dry run: {len(plan)} permit(s) in {jurisdiction}")
            for p in plan:
                typer.echo(f"  {p['permit_number']:<14} {p['action']:<22} {' -> '.join(p['strategies'])}")
        return

    from permit_trade_skills.detail.runs import format_report

    report = svc.fetch_batch(jurisdiction, permits, run_id=run_id, params=params, concurrency=concurrency,
                             force=force, resume=bool(resume))
    typer.echo(json.dumps(_jsonable(report), indent=2) if as_json else format_report(report))
    if report["failed"] and not report["ok"] and not report["skipped"]:
        raise typer.Exit(1)


@cli.command("show")
def show(
    permit: str = typer.Option(..., "--permit", "-p"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Print the latest stored snapshot: content_text, fields and provenance."""
    doc = _service(db_url).latest(permit, jurisdiction)
    if doc is None:
        typer.echo(f"No detail snapshot for {permit} in {jurisdiction} (run `pts detail fetch -p {permit}`)",
                   err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(_jsonable(doc.to_dict()), indent=2))
        return
    sig = doc.signals or {}
    typer.echo(f"Snapshot {doc.snapshot_id} | {doc.fetch_strategy} | fetched {doc.fetched_at:%Y-%m-%d %H:%M}Z"
               f" | {'DEGRADED | ' if doc.degraded else ''}signals {sig.get('count')}/{sig.get('min_signals')}")
    typer.echo(f"Source:  {doc.final_url}")
    typer.echo(f"Hash:    {doc.content_hash}")
    for note in doc.notes:
        typer.echo(f"Note:    {note}")
    typer.echo("--- content_text ---")
    typer.echo(doc.content_text.rstrip())
    typer.echo("--- fields ---")
    fields = {k: v for k, v in doc.fields.items() if k != "portal_pairs"}
    typer.echo(json.dumps(fields, indent=2))


@cli.command("report")
def report(
    run: str = typer.Option(..., "--run", help="Run id from `pts detail fetch`"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Summarize a fetch run: ok/failed/skipped, per strategy, per permit."""
    from permit_trade_skills.detail.runs import build_report, format_report

    svc = _service(db_url)
    row = svc.store.get_run(run)
    if row is None:
        typer.echo(f"Unknown run {run!r}", err=True)
        raise typer.Exit(1)
    rep = build_report(row, svc.store.run_results(run))
    typer.echo(json.dumps(_jsonable(rep), indent=2) if as_json else format_report(rep))


@cli.command("revalidate")
def revalidate(
    permit: str = typer.Option(..., "--permit", "-p"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Re-run F2 validation + normalization on the stored raw content (no network, no writes)."""
    from permit_trade_skills.detail.service import DetailUnavailable, PermitNotFound

    try:
        result = _service(db_url).revalidate(permit, jurisdiction)
    except (DetailUnavailable, PermitNotFound) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(json.dumps(_jsonable(result), indent=2))
    if result.get("revalidated") and not result["signals"]["passed"]:
        raise typer.Exit(1)


@cli.command("pick-live")
def pick_live(
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    count: int = typer.Option(1, "--count", "-n"),
    db_url: str = db_url_option(),
) -> None:
    """Print the most recently issued permit number(s) with a detail URL (via the query API)."""
    from permit_trade_skills.detail.live import pick_live_permits
    from permit_trade_skills.query.service import QueryService

    for number in pick_live_permits(QueryService(db_url), jurisdiction, count):
        typer.echo(number)

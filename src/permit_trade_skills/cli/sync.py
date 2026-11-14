"""CLI commands for permit ingestion and sync operations."""

import typer
from permit_trade_skills.config import db_url_option
from typing import Optional
from datetime import datetime

from permit_trade_skills.ingest.sync import SyncEngine


cli = typer.Typer(
    name="sync",
    help="Permit ingestion and synchronization commands",
)


def _parse_since(value: Optional[str]):
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise typer.BadParameter("expected a date as YYYY-MM-DD", param_hint="--since")


def _full_guard(engine: SyncEngine, jurisdiction: str) -> None:
    """--full on a Socrata sync re-pulls the whole dataset: estimate it, then require --yes."""
    import asyncio

    from permit_trade_skills.ingest.soda_client import SodaClient, SourceUnavailable

    try:
        rows = asyncio.run(engine.count_source_rows(jurisdiction))
        pages = -(-rows // SodaClient.MAX_LIMIT)
        typer.echo(f"--full re-pulls the whole dataset: about {rows:,} rows in {pages:,} requests "
                   f"(rate-limited), plus embeddings for new or changed descriptions.", err=True)
    except (SourceUnavailable, ValueError, LookupError) as e:
        typer.echo(f"--full re-pulls the whole dataset (could not estimate its size: {e}).", err=True)
    typer.echo("Re-run with --yes to proceed.", err=True)
    raise typer.Exit(code=1)


@cli.command(name="sync")
def sync_command(
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug (e.g., 'seattle-kingcounty-wa')"),
    db_url: str = db_url_option(),
    full: bool = typer.Option(False, "--full", "-f",
                              help="Pull the whole dataset (no cursor/window) and reprocess it even "
                                   "if identical to the last run; Socrata syncs need --yes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm a --full Socrata sync"),
    since: Optional[str] = typer.Option(None, "--since",
                                        help="Socrata: pull rows applied on/after YYYY-MM-DD "
                                             "(replaces the default 90-day window start)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n",
                                 help="Pull, normalize and count what would change; write nothing"),
    limit: int = typer.Option(None, "--limit", "-l", help="Maximum number of rows to process"),
    local_file: str = typer.Option(None, "--local-file", help="Path to local JSON file with permit data (bypasses Socrata)"),
) -> None:
    """
    Sync permits from the jurisdiction's source into the canonical store.

    By default, an incremental sync: rows past the cursor plus rows applied in
    the last 90 days. Use --full (with --yes) for a complete backfill.
    """
    import asyncio

    since_date = _parse_since(since)
    engine = SyncEngine(db_url=db_url)
    if full and not local_file and not yes:
        _full_guard(engine, jurisdiction)

    async def run():
        return await engine.sync(
            jurisdiction_slug=jurisdiction,
            full=full,
            dry_run=dry_run,
            limit=limit,
            local_file=local_file,
            since=since_date,
        )

    try:
        report = asyncio.run(run())
    except (LookupError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)

    outcome = "failed" if report.error else ("dry run complete (nothing written)" if dry_run else "complete")
    typer.echo(f"Sync {outcome} for {report.jurisdiction_id}")
    typer.echo(f"Run ID: {report.run_id}")
    typer.echo(f"Mode: {report.mode}")
    typer.echo(f"Config version: {report.config_version}")
    typer.echo(f"Duration: {report.duration_ms}ms")
    typer.echo(f"Pull count: {report.pull_count}")
    prefix = "Would be " if dry_run else ""
    typer.echo(f"{prefix}Inserted: {report.inserted}")
    typer.echo(f"{prefix}Updated: {report.updated}")
    typer.echo(f"Unchanged: {report.unchanged}")
    typer.echo(f"Dropped: {report.dropped}")
    if report.status_transitions:
        typer.echo("Status transitions: " + ", ".join(
            f"{k} {v}" for k, v in sorted(report.status_transitions.items())))
    if report.cursor_before or report.cursor_after:
        typer.echo(f"Cursor: {report.cursor_before} -> {report.cursor_after}")

    if report.warnings:
        typer.echo(f"\nWarnings ({len(report.warnings)}):")
        for warning in report.warnings:
            typer.echo(f"  - {warning.get('type', 'unknown')}: {warning.get('message', '')}")

    if report.error:
        typer.echo(f"\nError: {report.error}")
        raise typer.Exit(code=1)


def _report_dict(report) -> dict:
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {
        'run_id': report.run_id,
        'mode': report.mode,
        'config_version': report.config_version,
        'started_at': iso(report.started_at),
        'completed_at': iso(report.completed_at),
        'duration_ms': report.duration_ms,
        'pull_count': report.pull_count,
        'inserted': report.inserted,
        'updated': report.updated,
        'unchanged': report.unchanged,
        'dropped': report.dropped,
        'status_transitions': report.status_transitions or {},
        'cursor_before': report.cursor_before,
        'cursor_after': report.cursor_after,
        'max_applied_at': iso(report.max_applied_at),
        'max_synced_at': iso(report.max_synced_at),
        'fingerprint': report.fingerprint,
        'warnings': report.warnings or [],
        'error': report.error,
    }


@cli.command(name="sync-report")
def sync_report_command(
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug"),
    db_url: str = db_url_option(),
    last: int = typer.Option(10, "--last", "-l", help="Number of recent reports to show"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file for JSON report"),
) -> None:
    """
    Show sync reports for a jurisdiction (newest first): counts, mode, config
    version, cursors, status transitions and warnings.
    """
    import json

    from permit_trade_skills.discovery.registry import SyncMetadataTable

    engine = SyncEngine(db_url=db_url)
    session = engine.registry.get_session()
    try:
        try:
            jurisdiction_obj = engine.registry.resolve_jurisdiction(session, jurisdiction)
        except LookupError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1)

        reports = session.query(SyncMetadataTable).filter_by(
            jurisdiction_id=jurisdiction_obj.id
        ).order_by(SyncMetadataTable.completed_at.desc()).limit(last).all()
        report_list = [_report_dict(r) for r in reports]
    finally:
        session.close()

    if not report_list:
        typer.echo(f"No sync reports found for {jurisdiction_obj.id}")
        return

    if output:
        with open(output, 'w') as f:
            json.dump(report_list, f, indent=2)
        typer.echo(f"Reports saved to {output}")
        return

    typer.echo(f"Recent sync reports for {jurisdiction_obj.id} (last {len(report_list)}):")
    for i, r in enumerate(report_list, 1):
        typer.echo(f"\nReport {i}:")
        typer.echo(f"  Run ID: {r['run_id']}")
        typer.echo(f"  Mode: {r['mode']}, config version {r['config_version']}")
        typer.echo(f"  Completed: {r['completed_at']}")
        typer.echo(f"  Duration: {r['duration_ms']}ms")
        typer.echo(f"  Pull: {r['pull_count']}, Inserted: {r['inserted']}, Updated: {r['updated']}, "
                   f"Unchanged: {r['unchanged']}, Dropped: {r['dropped']}")
        if r['cursor_before'] or r['cursor_after']:
            typer.echo(f"  Cursor: {r['cursor_before']} -> {r['cursor_after']}")
        if r['status_transitions']:
            typer.echo("  Status transitions: " + ", ".join(
                f"{k} {v}" for k, v in sorted(r['status_transitions'].items())))
        for w in r['warnings']:
            detail = w.get('message') or ", ".join(f"{k}={v}" for k, v in w.items() if k != 'type')
            typer.echo(f"  Warning: {w.get('type', 'unknown')}: {detail}")
        if r['error']:
            typer.echo(f"  Error: {r['error']}")

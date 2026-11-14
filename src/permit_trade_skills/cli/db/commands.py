"""`pts db`: schema migrations, integrity checks, backup and retention.

All schema changes go through Alembic (see permit_trade_skills.store.schema and
docs/ops.md). Destructive revisions (docstring line `--destructive`) are refused
unless `pts db migrate --destructive` is used.
"""

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="db", help="Database schema, integrity, backup and retention commands")


def _engine(db_url: str):
    from sqlalchemy import create_engine

    return create_engine(db_url)


@cli.command(name="migrate")
def migrate_command(
    db_url: str = db_url_option(),
    revision: str = typer.Option("head", "--revision", "-r", help="Target revision"),
    destructive: bool = typer.Option(
        False, "--destructive",
        help="Allow revisions marked --destructive (drops data). Back up first: `pts db backup`.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt for --destructive"),
):
    """Apply pending migrations (upgrade to head). Refuses destructive revisions by default."""
    from permit_trade_skills.store import schema

    engine = _engine(db_url)
    st = schema.status(engine)
    if st.pre_alembic:
        typer.echo("Pre-Alembic database detected: stamping baseline 0001_initial.")
    if st.destructive_pending:
        if not destructive:
            typer.echo(
                "Refusing to apply destructive revision(s): " + ", ".join(st.destructive_pending)
                + "\n1. Back up first:  pts db backup\n2. Then re-run:     pts db migrate --destructive",
                err=True,
            )
            raise typer.Exit(code=2)
        typer.echo("DESTRUCTIVE revision(s) pending: " + ", ".join(st.destructive_pending))
        typer.echo("These drop data. Make sure you have a backup (`pts db backup`).")
        if not yes:
            typer.confirm("Apply them now?", abort=True)
    applied = schema.upgrade(engine, revision, allow_destructive=destructive)
    if applied:
        typer.echo(f"Applied {len(applied)} migration(s):")
        for rev in applied:
            typer.echo(f"  - {rev}")
    else:
        typer.echo("Database is up to date.")


@cli.command(name="upgrade")
def upgrade_command(
    revision: str = typer.Option("head", "--revision", "-r", help="Target migration revision"),
    db_url: str = db_url_option(),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be applied without applying"),
):
    """Upgrade to a revision (same safety rules as `migrate`; no --destructive)."""
    from permit_trade_skills.store import schema

    engine = _engine(db_url)
    if dry_run:
        st = schema.status(engine)
        typer.echo(f"Would apply: {', '.join(st.pending) or 'nothing'}")
        return
    try:
        applied = schema.upgrade(engine, revision)
    except schema.DestructiveMigrationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2)
    typer.echo(f"Applied: {', '.join(applied) or 'nothing (up to date)'}")


@cli.command(name="downgrade")
def downgrade_command(
    revision: str = typer.Option("-1", "--revision", "-r", help="Target revision (default: one step back)"),
    db_url: str = db_url_option(),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Downgrade the schema (drops the columns/tables added by the reverted revisions)."""
    from permit_trade_skills.store import schema

    engine = _engine(db_url)
    if not yes:
        typer.confirm(
            f"Downgrade to {revision}? Reverted revisions drop their columns/tables and data. "
            "Back up first with `pts db backup`.", abort=True,
        )
    schema.downgrade(engine, revision)
    typer.echo(f"Now at: {schema.status(engine).current}")


@cli.command(name="status")
def status_command(db_url: str = db_url_option()):
    """Show the current revision, head, and pending (incl. destructive) revisions."""
    from permit_trade_skills.store import schema

    st = schema.status(_engine(db_url))
    typer.echo("Migration Status")
    typer.echo("================")
    typer.echo(f"Current revision: {st.current or ('none - pre-Alembic schema' if st.pre_alembic else 'none (empty database)')}")
    typer.echo(f"Head revision:    {st.head}")
    typer.echo(f"Heads:            {', '.join(schema.heads())}")
    typer.echo(f"Pending:          {', '.join(st.pending) or 'none'}")
    if st.destructive_pending:
        typer.echo(f"Destructive:      {', '.join(st.destructive_pending)}")


@cli.command(name="revision")
def revision_command(
    message: str = typer.Option(..., "--message", "-m", help="Revision message"),
    autogenerate: bool = typer.Option(
        False, "--autogenerate", help="Draft operations by diffing the models against the database"),
    rev_id: Optional[str] = typer.Option(
        None, "--rev-id", help="Revision id, e.g. 0003_orchestration (the agreed chain name)"),
    db_url: str = db_url_option(),
):
    """Create a migration file. Autogenerated output is a DRAFT: review and edit it."""
    from permit_trade_skills.store import schema

    script = schema.new_revision(db_url, message, autogenerate=autogenerate, rev_id=rev_id)
    typer.echo(f"Created {script.path}")
    typer.echo("Review it: add extensions/CHECKs/partial indexes autogenerate can't detect, "
               "verify downgrade(), and mark data-dropping revisions --destructive.")


@cli.command(name="create", hidden=True)
def create_command(
    message: str = typer.Argument(..., help="Migration message/description"),
    db_url: str = db_url_option(),
):
    """Deprecated alias of `pts db revision -m MESSAGE` (empty revision)."""
    from permit_trade_skills.store import schema

    script = schema.new_revision(db_url, message)
    typer.echo(f"Created {script.path}")


@cli.command(name="doctor")
def doctor_command(
    db_url: str = db_url_option(),
    sample: int = typer.Option(1000, "--sample", help="Permits sampled for the raw_hash check"),
):
    """Integrity checks. Exits 1 if any hard check fails (CI-friendly)."""
    from permit_trade_skills.store.doctor import HARD, run_doctor

    report = run_doctor(_engine(db_url), sample_size=sample)
    for c in report.checks:
        mark = "OK  " if not c.failed else ("FAIL" if c.severity == HARD else "WARN")
        typer.echo(f"[{mark}] {c.name}: {c.count} - {c.description}")
        for ex in c.examples[:5]:
            typer.echo(f"         {ex}")
    if not report.ok:
        typer.echo(f"\n{len(report.hard_failures)} hard failure(s).", err=True)
        raise typer.Exit(code=1)
    typer.echo("\nNo hard failures.")


@cli.command(name="backup")
def backup_command(
    db_url: str = db_url_option(),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Dump file (default: backups/<db>-<timestamp>.dump)"),
):
    """Full pg_dump (custom format) of the database. Restore with pg_restore."""
    from sqlalchemy.engine import make_url

    url = make_url(db_url).set(drivername="postgresql")
    if output is None:
        output = Path("backups") / f"{url.database}-{datetime.now():%Y%m%d-%H%M%S}.dump"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", f"--file={output}",
             url.render_as_string(hide_password=False)],
            check=True,
        )
    except FileNotFoundError:
        typer.echo("pg_dump not found; install the PostgreSQL client tools.", err=True)
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as e:
        typer.echo(f"pg_dump failed (exit {e.returncode}).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Backup written to {output} ({output.stat().st_size:,} bytes)")
    typer.echo(f"Restore: pg_restore --clean --if-exists -d <db_url> {output}")


@cli.command(name="prune")
def prune_command(
    db_url: str = db_url_option(),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only report what would be pruned"),
    raw_html_days: int = typer.Option(90, "--raw-html-days", help="NULL detail_snapshots.raw_html older than this"),
    keep_runs: int = typer.Option(512, "--keep-runs", help="sync_metadata runs kept per jurisdiction"),
):
    """Apply the retention policy (raw_html after 90 days; keep the latest 512 sync runs; expired NLU cache)."""
    from permit_trade_skills.store import transaction
    from permit_trade_skills.store.retention import prune

    with transaction(db_url) as session:
        result = prune(session, raw_html_days, keep_runs, dry_run=dry_run)
    verb = "Would prune" if dry_run else "Pruned"
    typer.echo(f"{verb}: raw_html NULLed on {result.raw_html_nulled} snapshot(s); "
               f"{result.sync_runs_deleted} old sync run(s) deleted; "
               f"{result.nlu_cache_deleted} expired NLU cache row(s) deleted.")

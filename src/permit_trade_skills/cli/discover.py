"""CLI commands for discovery operations."""

import typer
from permit_trade_skills.config import db_url_option
from typing import Optional
from pathlib import Path

from permit_trade_skills.discovery import DiscoveryEngine


cli = typer.Typer(
    name="discovery",
    help="Catalog discovery and jurisdiction registration commands",
)

DEFAULT_EVIDENCE_DIR = "evals/evidence"
TABLE_ROWS = 20


def _ranked_table(dossier) -> None:
    """Print the ranked candidates (top TABLE_ROWS) with their score breakdown."""
    candidates = dossier.candidates
    if not candidates:
        return
    typer.echo(f"\n{'#':>3}  {'score':>5}  {'platform':<8}  {'id':<38}  title")
    for rank, c in enumerate(candidates[:TABLE_ROWS], 1):
        platform = c.platform_type.value if c.platform_type else "-"
        typer.echo(f"{rank:>3}  {c.confidence:>5.2f}  {platform:<8}  {c.id[:38]:<38}  {c.title[:60]}")
    if len(candidates) > TABLE_ROWS:
        typer.echo(f"  ... {len(candidates) - TABLE_ROWS} more in the dossier")
    top = candidates[0]
    if top.score_breakdown:
        parts = ", ".join(f"{name} {f['score']:.2f}×{f['weight']}"
                          for name, f in top.score_breakdown.items())
        typer.echo(f"\nTop candidate breakdown: {parts}")


@cli.command(name="discover")
def discover_command(
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction name (e.g., 'Seattle')"),
    state: str = typer.Option(..., "--state", "-s", help="State abbreviation (e.g., 'WA')"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path for dossier"),
    query: Optional[str] = typer.Option(None, "--query", "-q",
                                        help="Catalog search to run instead of the default searches "
                                             "on the jurisdiction name (e.g. '\"building permits\" WA')"),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", min=1,
                                        help="Maximum candidates to collect (default 1000)"),
    evidence_dir: str = typer.Option(DEFAULT_EVIDENCE_DIR, "--evidence-dir",
                                     help="Where to mirror the dossier as audit evidence "
                                          "(<dir>/<jurisdiction-id>/discovery/<date>-<run>.json)"),
    db_url: str = db_url_option(),
    local_file: Optional[str] = typer.Option(None, "--local-file", help="Path to local JSON file with permit data (bypasses catalog.data.gov)"),
) -> None:
    """
    Discover datasets for a jurisdiction on catalog.data.gov.

    Use --local-file to load permit data from a local JSON file instead of
    searching catalog.data.gov. This is useful when the catalog API is unavailable.
    Every run is recorded in discovery_runs and mirrored to --evidence-dir.
    """
    import asyncio
    import json

    output_path = Path(output) if output else None

    async def run():
        engine = DiscoveryEngine(db_url=db_url)

        if local_file:
            local_path = Path(local_file)
            if not local_path.exists():
                typer.echo(f"Error: File not found: {local_file}", err=True)
                raise typer.Exit(code=1)

            with open(local_path, "r") as f:
                permit_data = json.load(f)

            return await engine.discover_from_local_file(
                permit_data=permit_data,
                jurisdiction_name=jurisdiction,
                state=state,
                output_path=output_path,
                source_path=str(local_path),
                evidence_dir=Path(evidence_dir),
            )
        return await engine.discover(jurisdiction, state, output_path, query=query, limit=limit,
                                     evidence_dir=Path(evidence_dir))

    dossier = asyncio.run(run())

    typer.echo(f"Discovery complete for {jurisdiction}, {state} (run {dossier.discovery_run_id})")
    typer.echo(f"Found {len(dossier.candidates)} candidates")
    typer.echo(f"Verification results: {len(dossier.verification_results)} probes")
    _ranked_table(dossier)
    if dossier.notes:
        typer.echo(f"\nNotes: {dossier.notes}")
    if output_path:
        typer.echo(f"Dossier: {output_path}")


@cli.command(name="register")
def register_command(
    dossier_path: str = typer.Option(..., "--dossier", "-d", help="Path to discovery dossier JSON"),
    name: str = typer.Option(..., "--name", "-n", help="Jurisdiction name"),
    state: str = typer.Option(..., "--state", "-s", help="State abbreviation"),
    county: Optional[str] = typer.Option(None, "--county", "-c", help="County name"),
    approve: Optional[str] = typer.Option(None, "--approve",
                                          help="Register this candidate id from the dossier instead "
                                               "of the top-ranked one"),
    replace: bool = typer.Option(False, "--replace",
                                 help="Overwrite an already registered jurisdiction with the same id "
                                      "(replaces its field map, status map and detail strategy)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Print the config that would be registered and the checks; write nothing"),
    force: bool = typer.Option(False, "--force", help="Register despite validation errors"),
    check_columns: bool = typer.Option(False, "--check-columns",
                                       help="Also check mapped columns against the dataset's live "
                                            "columns.json (needs base_url and dataset_id)"),
    output_db: str = db_url_option(),
) -> None:
    """
    Register a discovered jurisdiction in the database, with the dossier's
    field map, status map and detail strategy.
    """
    register_from_dossier_file(dossier_path, name, state, county, output_db, replace,
                               approve=approve, dry_run=dry_run, force=force,
                               check_columns=check_columns)


def print_validation(result) -> None:
    for message in result.errors:
        typer.echo(f"  error: {message}", err=True)
    for message in result.warnings:
        typer.echo(f"  warning: {message}", err=True)


def check_config(field_map, status_map, detail_strategy, base_url, dataset_id, check_columns: bool):
    """validate_config, plus the live columns check when asked and possible."""
    import asyncio

    from permit_trade_skills.discovery.validate import validate_config
    from permit_trade_skills.ingest.soda_client import SourceUnavailable, fetch_columns

    columns = None
    skipped = None
    if check_columns:
        if base_url and dataset_id:
            try:
                columns = asyncio.run(fetch_columns(base_url, dataset_id))
            except SourceUnavailable as e:
                skipped = f"columns check failed: {e}"
        else:
            skipped = "columns check skipped: the jurisdiction has no base_url/dataset_id"
    result = validate_config(field_map, status_map, detail_strategy, columns=columns)
    if skipped:
        (result.errors if skipped.startswith("columns check failed") else result.warnings).append(skipped)
    return result


def register_from_dossier_file(dossier_path: str, name: str, state: str, county: Optional[str],
                               db_url: str, replace: bool, approve: Optional[str] = None,
                               dry_run: bool = False, force: bool = False,
                               check_columns: bool = False) -> None:
    """Shared by `pts discovery register` and `pts register jurisdiction`."""
    import asyncio
    import json

    from permit_trade_skills.discovery import JurisdictionExistsError
    from permit_trade_skills.discovery.schemas import DiscoveryDossier

    with open(dossier_path, "r") as f:
        dossier = DiscoveryDossier(**json.load(f))

    if approve is not None:
        chosen = next((c for c in dossier.candidates if c.id == approve), None)
        if chosen is None:
            ids = ", ".join(c.id for c in dossier.candidates[:20]) or "none"
            typer.echo(f"Error: candidate {approve!r} is not in the dossier (candidates: {ids})", err=True)
            raise typer.Exit(code=1)
        dossier.selected_candidate = chosen

    jurisdiction = DiscoveryEngine.jurisdiction_from_dossier(dossier, name, state, county)
    validation = check_config(jurisdiction.field_map, jurisdiction.status_map,
                              jurisdiction.detail_strategy, jurisdiction.base_url,
                              jurisdiction.dataset_id, check_columns)
    engine = DiscoveryEngine(db_url=db_url)

    if dry_run:
        session = engine.registry.get_session()
        try:
            existing = engine.registry.get_jurisdiction(session, jurisdiction.id)
            existing_version = existing.config_version if existing else None
        finally:
            session.close()
        if existing is not None and not replace:
            action = "refused: already registered (pass --replace)"
        elif not validation.ok and not force:
            action = "refused: validation errors (pass --force)"
        else:
            action = f"replace (config_version {existing_version} -> {existing_version + 1})" \
                if existing is not None else "insert (config_version 1)"
        candidate = dossier.selected_candidate
        plan = {
            "action": action,
            "jurisdiction": jurisdiction.model_dump(mode="json", exclude={"api_key", "created_at",
                                                                         "updated_at", "config_version"}),
            "candidate": ({"id": candidate.id, "title": candidate.title, "url": candidate.url,
                           "confidence": candidate.confidence,
                           "score_breakdown": candidate.score_breakdown} if candidate else None),
            "probes": [p.model_dump(mode="json") for p in dossier.verification_results],
            "discovery_run_id": dossier.discovery_run_id,
            "validation": validation.as_dict(),
        }
        typer.echo(json.dumps(plan, indent=2, default=str))
        if action.startswith("refused"):
            raise typer.Exit(code=1)
        return

    print_validation(validation)
    if not validation.ok and not force:
        typer.echo("Error: the config has validation errors; fix the dossier or pass --force.", err=True)
        raise typer.Exit(code=1)

    try:
        jurisdiction_id = asyncio.run(engine.register(dossier, jurisdiction, replace=replace))
    except JurisdictionExistsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Jurisdiction registered: {jurisdiction_id}")

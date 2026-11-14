"""Config CLI commands."""

import typer
from permit_trade_skills.config import db_url_option
from typing import Optional
from pathlib import Path

cli = typer.Typer(
    name="config",
    help="Configuration management commands",
)

DEFAULT_EXPORT_DIR = "config/jurisdictions"


@cli.command(name="export")
def export_config(
    jurisdiction_id: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction ID"),
    output: Optional[str] = typer.Option(None, "--output", "-o",
                                         help=f"Output YAML file path (default {DEFAULT_EXPORT_DIR}/<id>.yaml)"),
    db_url: str = db_url_option(),
) -> None:
    """
    Export jurisdiction configuration to YAML.
    """
    from permit_trade_skills.discovery import DiscoveryEngine

    engine = DiscoveryEngine(db_url=db_url)
    output_path = Path(output) if output else Path(DEFAULT_EXPORT_DIR) / f"{jurisdiction_id}.yaml"

    if not engine.export_config(jurisdiction_id, output_path):
        typer.echo(f"Jurisdiction not found: {jurisdiction_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Config exported to: {output_path}")


@cli.command(name="history")
def config_history(
    jurisdiction_id: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction ID"),
    version: Optional[int] = typer.Option(None, "--version", "-v",
                                          help="Print this config_version as YAML instead of the list"),
    db_url: str = db_url_option(),
) -> None:
    """
    List a jurisdiction's config versions (current and replaced), or print one.
    """
    import yaml

    from permit_trade_skills.discovery.registry import Registry

    registry = Registry(database_url=db_url)
    session = registry.get_session()
    try:
        current = registry.get_jurisdiction(session, jurisdiction_id)
        if current is None:
            typer.echo(f"Jurisdiction not found: {jurisdiction_id}", err=True)
            raise typer.Exit(code=1)
        versions = registry.list_versions(session, jurisdiction_id)
        current_config = Registry.config_snapshot(current)
        current_version = current.config_version
    finally:
        session.close()

    if version is not None:
        if version == current_version:
            config = current_config
        else:
            match = next((v for v in versions if v.config_version == version), None)
            if match is None:
                typer.echo(f"No config_version {version} for {jurisdiction_id} "
                           f"(known: {[v.config_version for v in versions] + [current_version]})", err=True)
                raise typer.Exit(code=1)
            config = match.config
        typer.echo(yaml.dump({"id": jurisdiction_id, "config_version": version, **config},
                             default_flow_style=False, sort_keys=False), nl=False)
        return

    typer.echo(f"{'version':>7}  {'replaced_at':<19}  replaced_by")
    for v in versions:
        replaced_at = v.created_at.strftime("%Y-%m-%d %H:%M:%S") if v.created_at else "-"
        typer.echo(f"{v.config_version:>7}  {replaced_at:<19}  {v.replaced_by or '-'}")
    typer.echo(f"{current_version:>7}  {'(current)':<19}")

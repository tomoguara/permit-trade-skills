"""`pts quality ...`: the Stage 08 release gate and its owner sign-off."""

from __future__ import annotations

import json
from typing import Any, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="quality", help="Release gate: pass/fail a build on the quality thresholds")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


@cli.command("gate")
def gate_command(
    build: Optional[str] = typer.Option(None, "--build", "-b", help="Build id (default: the git commit)"),
    skip: Optional[List[str]] = typer.Option(None, "--skip", help="Criterion or suite to exclude (recorded)"),
    slo_window: Optional[str] = typer.Option("24h", "--slo-window", help="Include the SLO report ('' to omit)"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Aggregate the build's newest H1-H7 runs against eval.thresholds. Exit 0 PASS, 1 FAIL, 2 CONDITIONAL."""
    from permit_trade_skills.eval import gate, slo
    from permit_trade_skills.eval.config import default_build_id, window_delta
    from permit_trade_skills.wiring import default_jurisdiction, eval_store

    store = eval_store(db_url)
    build_id = build or default_build_id()
    slo_report = None
    if slo_window:
        try:
            slo_report = slo.report(store, default_jurisdiction(db_url, jurisdiction), window_delta(slo_window))
        except ValueError as e:
            typer.echo(f"SLO omitted: {e}", err=True)
    report = gate.evaluate(store, build_id, skip=list(skip or []), slo=slo_report)
    _print_json(report) if as_json else typer.echo(gate.format_report(report))
    raise typer.Exit(gate.EXIT_CODES[report["status"]])


@cli.command("show")
def show_command(
    build: Optional[str] = typer.Option(None, "--build", "-b"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """The newest stored gate report of a build (and its sign-off)."""
    from permit_trade_skills.eval import gate
    from permit_trade_skills.eval.config import default_build_id
    from permit_trade_skills.wiring import eval_store

    row = eval_store(db_url).latest_gate(build or default_build_id())
    if row is None:
        typer.echo("no gate report for this build", err=True)
        raise typer.Exit(1)
    if as_json:
        _print_json(row)
        return
    typer.echo(gate.format_report(row["report"]))
    if row.get("signed_off_by"):
        typer.echo(f"\nSigned off by {row['signed_off_by']} at {row['signed_off_at']}: {row['sign_off_note']}")


@cli.command("signoff")
def signoff_command(
    build: Optional[str] = typer.Option(None, "--build", "-b"),
    note: str = typer.Option(..., "--note", help="Why promotion is acceptable despite the soft breach"),
    db_url: str = db_url_option(),
) -> None:
    """Owner sign-off of a CONDITIONAL gate (owner from PTS_REVIEWER)."""
    from permit_trade_skills.eval import gate
    from permit_trade_skills.eval.config import default_build_id
    from permit_trade_skills.orchestration.review import ReviewError, reviewer_from_env
    from permit_trade_skills.wiring import eval_store

    try:
        owner = reviewer_from_env()
    except ReviewError as e:
        typer.echo(e.message, err=True)
        raise typer.Exit(2) from e
    try:
        row = gate.sign_off(eval_store(db_url), build or default_build_id(), owner.name, note)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Build {row['build_id']}: CONDITIONAL signed off by {row['signed_off_by']}; promotable.")

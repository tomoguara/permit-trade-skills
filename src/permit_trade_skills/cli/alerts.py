"""`pts alerts ...`: Stage 08 drift detectors (run nightly) and the alert list."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="alerts", help="Drift detection and alerts")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


@cli.command("check")
def check_command(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="POST new alerts to eval.drift.webhook_url"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Run the drift detectors; a breach writes a drift_alerts row. Exit 3 when an alert was raised."""
    from permit_trade_skills.eval import drift
    from permit_trade_skills.settings import load_settings
    from permit_trade_skills.wiring import default_jurisdiction, eval_store

    settings = load_settings()
    url = settings["eval"]["drift"].get("webhook_url")
    result = drift.check(eval_store(db_url), default_jurisdiction(db_url, jurisdiction), settings=settings,
                         notify=drift.webhook_notifier(url) if notify and url else None)
    if as_json:
        _print_json(result)
    else:
        for d in result["detectors"]:
            if d.get("skipped"):
                typer.echo(f"{d['detector']:<17} skipped: {d['skipped']}")
            else:
                state = "BREACH" if d["breach"] else "ok"
                typer.echo(f"{d['detector']:<17} {state:<7} {d['message'] if d['breach'] else ''}")
        for a in result["alerts"]:
            typer.echo(f"alert #{a['id']} [{a['severity']}] {a['detector']}: suspected {a['suspected_cause']}")
    raise typer.Exit(3 if result["alerts"] else 0)


@cli.command("list")
def list_command(
    open_only: bool = typer.Option(False, "--open", help="Only unacknowledged alerts"),
    since: Optional[str] = typer.Option(None, "--since"),
    limit: int = typer.Option(20, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    from permit_trade_skills.wiring import eval_store

    rows = eval_store(db_url).alerts(since=datetime.fromisoformat(since) if since else None, open_only=open_only,
                                     limit=limit)
    if as_json:
        _print_json(rows)
        return
    if not rows:
        typer.echo("No alerts.")
    for a in rows:
        ack = f"  ack {a['acknowledged_by']}" if a.get("acknowledged_by") else ""
        typer.echo(f"#{a['id']} {a['created_at']:%Y-%m-%d %H:%M} [{a['severity']}] {a['detector']} "
                   f"(suspected {a['suspected_cause']}){ack}\n    {a['message']}")


@cli.command("ack")
def ack_command(
    alert_id: int = typer.Option(..., "--id"),
    db_url: str = db_url_option(),
) -> None:
    """Acknowledge an alert (name from PTS_REVIEWER)."""
    from permit_trade_skills.orchestration.review import ReviewError, reviewer_from_env
    from permit_trade_skills.wiring import eval_store

    try:
        who = reviewer_from_env()
    except ReviewError as e:
        typer.echo(e.message, err=True)
        raise typer.Exit(2) from e
    if not eval_store(db_url).mark_alert(alert_id, acknowledged_by=who.name):
        typer.echo(f"alert {alert_id} not found", err=True)
        raise typer.Exit(1)
    typer.echo(f"alert {alert_id} acknowledged by {who.name}")

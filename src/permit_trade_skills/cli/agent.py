"""`pts agent ...`: Stage 07 planner agent (docs/orchestration.md)."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="agent", help="Answer permit goals with the planner agent; replay a run")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def format_report(report: Dict[str, Any]) -> str:
    b = report["budget"]
    lines = [f"Goal: {report['goal']}",
             f"Run {report['run_id']}  jurisdiction={report['jurisdiction']}  {report['toolset_version']}  "
             f"model={report.get('model')}",
             f"Integrity: {report['integrity']}   confidence: {report['confidence']}   "
             f"turns {b['iterations']}/{b['max_iterations']}, tool calls {b['tool_calls']}/{b['max_tool_calls']}, "
             f"expensive {b['expensive_calls']}/{b['max_expensive_calls']}"]
    if report.get("stopped"):
        lines.append(f"STOPPED: {report['stopped']}")
    lines += ["", "Steps:"]
    for i, s in enumerate(report["steps"], 1):
        lines.append(f"  {i:>2}. {s['tool']} {json.dumps(s['params'], default=str)} -> "
                     f"{'ok' if s['ok'] else s.get('error_code')} ({s['latency_ms']} ms)")
    lines += ["", "Findings:"]
    for i, f in enumerate(report.get("findings") or []):
        lines.append(f"  [{i}] {f['permit_number']}  {f['status']}  detail={'yes' if f['detail_present'] else 'no'}")
        for s in f.get("skills") or []:
            lines.append(f"      - {s['skill']} ({s['trade']}, {s['confidence']}): \"{s['evidence']}\"")
    if not report.get("findings"):
        lines.append("  (none)")
    if report.get("unfulfilled"):
        lines += ["", "Unfulfilled:"]
        lines += [f"  - {u['requested']}: {u['reason']} -> {u['suggested_action']}" for u in report["unfulfilled"]]
    if report.get("citations"):
        lines += ["", "Citations: " + ", ".join(f"[{c['claim_index']}] {c['source']}" for c in report["citations"])]
    for v in report.get("violations") or []:
        lines.append(f"VIOLATION {v['type']}: {v.get('message')}")
    return "\n".join(lines)


@cli.command("goal")
def goal(
    text: str = typer.Argument(..., help='e.g. "issued residential additions over $100k this year, with skills"'),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j",
                                               help="Jurisdiction id (default: the only active one)"),
    context: str = typer.Option("", "--context", help="Extra context for the planner"),
    as_json: bool = typer.Option(False, "--json", help="Print the AgentReport as JSON"),
    db_url: str = db_url_option(),
) -> None:
    """Run the planner on one goal; prints a cited report (exit 3 if the integrity check fails)."""
    from permit_trade_skills.wiring import planner

    report = planner(db_url).run(text, jurisdiction=jurisdiction, context=context)
    _print_json(report) if as_json else typer.echo(format_report(report))
    if report["integrity"] == "FAILED":
        raise typer.Exit(3)


@cli.command("replay")
def replay(
    run_id: str = typer.Option(..., "--run", help="An agent run id (agent-...)"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Re-run a recorded agent run's tool calls against current data (read-only, no LLM) and diff."""
    from permit_trade_skills.orchestration.replay import replay as do_replay
    from permit_trade_skills.wiring import agent_toolset, orchestration_store

    result = do_replay(run_id, orchestration_store(db_url), agent_toolset(db_url))
    if result is None:
        typer.echo(f"no agent run {run_id!r}", err=True)
        raise typer.Exit(1)
    if as_json:
        _print_json(result)
        return
    typer.echo(f"Replay of {run_id} ({result['recorded_toolset_version']} -> {result['toolset_version']}): "
               f"{result['calls']} call(s): {result['identical']} identical, {result['changed']} changed, "
               f"{result['skipped']} skipped")
    for r in result["replay_diff"]:
        how = f" as {r['replayed_as']}" if r.get("replayed_as") and r["replayed_as"] != r["tool"] else ""
        extra = r.get("reason") or (json.dumps(r["diff"]) if r.get("diff") else "")
        typer.echo(f"  {r['index']:>2}. {r['tool']}{how} {json.dumps(r['params'], default=str)}: {r['status']} "
                   f"{extra}".rstrip(), file=sys.stdout)

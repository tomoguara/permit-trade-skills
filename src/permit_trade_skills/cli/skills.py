"""`pts skills ...`: Stage 06 trade-skill inference (docs/trade-skills.md)."""

from __future__ import annotations

import json
import textwrap
from typing import Any, Dict, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="skills", help="Infer, batch and inspect cited trade-skill inferences")

DEFAULT_JURISDICTION = "seattle-kingcounty-wa"


def _service(db_url: str):
    from permit_trade_skills.wiring import skill_service

    return skill_service(db_url)


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _wrap(text: str, width: int, indent: str, rest: Optional[str] = None) -> str:
    return textwrap.fill(text or "", width=width, initial_indent=indent,
                         subsequent_indent=rest if rest is not None else indent)


def format_inference(inf: Dict[str, Any], table: Optional[List[Dict[str, Any]]] = None) -> str:
    """Human-readable inference; with `table`, one ✓/✗ grounding mark per skill."""
    lines = [f"{inf['permit_key']}  inference #{inf.get('inference_id')}  "
             f"{'(cached) ' if inf.get('cached') else ''}{inf['prompt_version']}  {inf['model']}",
             f"overall_confidence: {inf['overall_confidence']}   review_status: {inf['review_status']}"
             f"   degraded: {str(inf['degraded']).lower()}"]
    if inf.get("evidence_source"):
        lines.append(f"evidence_source: {inf['evidence_source']}")
    lines.append("")
    marks = {i: ("✓" if not t["reasons"] else "✗ " + ",".join(t["reasons"])) for i, t in enumerate(table or [])}
    for i, s in enumerate(inf["skills"], 1):
        mark = f"  {marks[i - 1]}" if table is not None else ""
        lines.append(f"{i:>2}. [{s['confidence']}] {s['skill']} — {s['trade']}{mark}")
        lines.append(_wrap(f"“{s['evidence']}”", 100, "      "))
    if inf.get("assumptions"):
        lines += ["", "Assumptions:"] + [_wrap(a, 100, "  - ", "    ") for a in inf["assumptions"]]
    if inf.get("licensing_notes"):
        lines += ["", "Licensing notes (informational, non-normative):"] + [_wrap(n, 100, "  - ", "    ") for n in inf["licensing_notes"]]
    if inf.get("dropped_skills"):
        lines += ["", "Dropped by the grounding gate:"]
        lines += [f"  - {d.get('skill')} ({', '.join(d.get('reasons', []))}): “{d.get('evidence')}”"
                  for d in inf["dropped_skills"]]
    check = inf.get("signal_check") or {}
    if check:
        sig = ", ".join(f"{t} ({v['strength']})" for t, v in (check.get("signals") or {}).items()) or "none"
        lines += ["", f"Signals: {sig}", f"Covered: {', '.join(check.get('covered') or []) or 'none'}"]
        if check.get("missing_high_signals"):
            lines.append(f"Missing high signals: {', '.join(check['missing_high_signals'])}")
    if inf.get("warnings"):
        lines += ["", "Warnings:"] + [f"  - {w}" for w in inf["warnings"]]
    return "\n".join(lines)


@cli.command("infer")
def infer(
    permit: List[str] = typer.Option(..., "--permit", "-p", help="Permit number (repeatable)"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    allow_degraded: bool = typer.Option(False, "--allow-degraded",
                                        help="No stored detail: infer from the catalog fields (low confidence)"),
    status: Optional[List[str]] = typer.Option(None, "--status",
                                               help="Eligible statuses (repeatable; default skills.allowed_statuses)"),
    as_json: bool = typer.Option(False, "--json", help="Print the inference(s) as JSON"),
    db_url: str = db_url_option(),
) -> None:
    """Infer the trade skills for one or more permits (uses the stored detail snapshot)."""
    from permit_trade_skills.skills.schemas import InferenceFailed, NotEligible
    from permit_trade_skills.skills.service import PermitNotFound

    svc = _service(db_url)
    out, failed = [], 0
    for number in permit:
        try:
            inf = svc.infer(number, jurisdiction, allow_degraded=allow_degraded, allowed_statuses=status)
        except PermitNotFound as e:
            typer.echo(str(e), err=True)
            failed += 1
            continue
        except NotEligible as e:
            typer.echo(f"Not eligible: {e}", err=True)
            failed += 1
            continue
        except InferenceFailed as e:
            typer.echo(f"Failed: {e}", err=True)
            if as_json:
                out.append({"permit_number": number, "error": e.state_error()})
            failed += 1
            continue
        if as_json:
            out.append(inf.to_dict())
        else:
            typer.echo(format_inference(inf.to_dict()))
            typer.echo("")
    if as_json:
        _print_json(out if len(permit) > 1 else (out[0] if out else None))
    if failed:
        raise typer.Exit(1)


@cli.command("batch")
def batch(
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    state: str = typer.Option("DETAIL_READY", "--state", help="Processing state of the cohort ('' = any)"),
    status: Optional[str] = typer.Option(None, "--status", help="Also filter by canonical status, e.g. ISSUED"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max permits (each is one LLM call unless cached)"),
    allow_degraded: bool = typer.Option(False, "--allow-degraded"),
    as_json: bool = typer.Option(False, "--json", help="Print the batch report as JSON"),
    db_url: str = db_url_option(),
) -> None:
    """Infer a cohort, e.g. every DETAIL_READY permit (orchestrator-friendly; sequential)."""
    svc = _service(db_url)
    report = svc.batch(jurisdiction, state=state or None, status=status, limit=limit,
                       allow_degraded=allow_degraded)
    if as_json:
        _print_json(report)
        return
    typer.echo(f"{report['total']} permit(s): {report['inferred']} inferred, {report['cached']} cached, "
               f"{report['failed']} failed, {report['not_eligible']} not eligible")
    for r in report["results"]:
        detail = (f"{r.get('skills')} skill(s), {r.get('overall_confidence')}"
                  + (f", {r['dropped']} dropped" if r.get("dropped") else "")
                  + (", degraded" if r.get("degraded") else "")
                  if r["outcome"] in ("inferred", "cached")
                  else r.get("error") or r.get("reason") or "")
        typer.echo(f"  {r['permit_number']:<14} {r['outcome']:<13} {detail}  ({r['duration_ms']} ms)")


@cli.command("show")
def show(
    permit: str = typer.Option(..., "--permit", "-p"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """The latest stored inference, each skill re-checked against the stored detail (✓/✗)."""
    from permit_trade_skills.skills.service import PermitNotFound

    svc = _service(db_url)
    try:
        result = svc.show(permit, jurisdiction)
    except PermitNotFound as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    if result is None:
        typer.echo(f"No skill inference stored for {permit} (run `pts skills infer --permit {permit}`)", err=True)
        raise typer.Exit(1)
    if as_json:
        _print_json(result)
        return
    typer.echo(format_inference(result["inference"], result["grounding"]))
    grounded = sum(1 for t in result["grounding"] if t["grounded"])
    typer.echo(f"\nGrounding: {grounded}/{len(result['grounding'])} skill(s) ✓ against the stored detail; "
               f"permit state {result['processing_state']}")

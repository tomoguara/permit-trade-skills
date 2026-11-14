"""`pts review ...`: Stage 07 human-in-the-loop review of skill inferences.

Decisions need the reviewer's name in PTS_REVIEWER (shell or .env). Agents
never set it: the planner's submit_review tool only proposes (NEEDS_HUMAN).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="review", help="Inspect skill inferences and approve, reject or flag them (HITL)")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _jurisdiction(db_url: str, jurisdiction: Optional[str]) -> str:
    from permit_trade_skills.wiring import default_jurisdiction

    try:
        return default_jurisdiction(db_url, jurisdiction)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _service(db_url: str):
    from permit_trade_skills.wiring import review_service

    return review_service(db_url)


def _reviewer():
    from permit_trade_skills.orchestration.review import ReviewError, reviewer_from_env

    try:
        return reviewer_from_env()
    except ReviewError as e:
        typer.echo(e.message, err=True)
        raise typer.Exit(2) from e


def format_queue(rows: List[Dict[str, Any]]) -> str:
    lines = [f"{'permit':<14} {'status':<10} {'state':<16} {'conf':<6} {'skills':>6} {'dropped':>7} "
             f"{'assump':>6} {'age_h':>7}  review"]
    for r in rows:
        proposal = f" ({r['proposed_by']}: {r['proposed_decision']})" if r.get("proposed_by") else ""
        lines.append(f"{r['permit_number']:<14} {r['status']:<10} {r['processing_state']:<16} "
                     f"{r['overall_confidence'] or '-':<6} {r['skills']:>6} {r['dropped']:>7} {r['assumptions']:>6} "
                     f"{r['age_hours'] if r['age_hours'] is not None else '-':>7}  {r['review_status']}{proposal}")
    return "\n".join(lines) if rows else "No inferences awaiting review."


def format_inspect(view: Dict[str, Any]) -> str:
    inf, review = view["inference"], view["review"]
    snap = view.get("detail_snapshot") or {}
    lines = [f"{view['permit_number']} ({view['jurisdiction']})  status {view['status']}  "
             f"state {view['processing_state']}",
             f"Review: {review['review_status']}" + (f" by {review['reviewer']} at {review['reviewed_at']}"
                                                      if review.get("reviewer") else "")
             + (f"  proposed by {review['proposed_by']}: {review['proposed_decision']}"
                if review.get("proposed_by") else ""),
             f"Model {inf.get('model')}  prompt {inf.get('prompt_version')}  "
             f"overall confidence {inf.get('overall_confidence')}" + ("  [degraded]" if inf.get("degraded") else ""),
             f"Detail: {snap.get('fetch_strategy', '-')} {snap.get('final_url') or view.get('evidence_source') or '-'}"
             f"  fetched {snap.get('fetched_at', '-')}"]
    if review.get("review_note"):
        lines.append(f"Note: {review['review_note']}")
    lines += ["", "Skills (grounded = evidence found verbatim in the stored detail):"]
    for g in view.get("grounding") or [{**s, "grounded": None} for s in inf.get("skills") or []]:
        mark = {True: "✓", False: "✗", None: "?"}[g.get("grounded")]
        lines.append(f"  {mark} {g.get('skill')} [{g.get('trade')}, {g.get('confidence')}]")
        lines.append(f"      evidence: \"{g.get('evidence')}\"")
    if inf.get("assumptions"):
        lines += ["", "Assumptions:"] + [f"  - {a}" for a in inf["assumptions"]]
    if inf.get("dropped_skills"):
        lines += ["", f"Dropped by the grounding gate: {len(inf['dropped_skills'])}"]
    return "\n".join(lines)


@cli.command("list")
def list_cmd(
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    state: str = typer.Option("SKILLS_INFERRED", "--state", help="Permit processing state ('' for any)"),
    confidence: Optional[str] = typer.Option(None, "--confidence", help="high | medium | low"),
    needs_human: bool = typer.Option(False, "--needs-human", help="Only inferences flagged NEEDS_HUMAN"),
    limit: int = typer.Option(50, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Inferences awaiting review (PENDING / NEEDS_HUMAN), oldest first."""
    rows = _service(db_url).queue(_jurisdiction(db_url, jurisdiction), state=state or None, confidence=confidence,
                                  needs_human=needs_human, limit=limit)
    _print_json(rows) if as_json else typer.echo(format_queue(rows))


@cli.command("inspect")
def inspect(
    permit: str = typer.Option(..., "--permit", "-p"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """The full inference: skills with evidence and ✓/✗ grounding, assumptions, detail provenance, review."""
    from permit_trade_skills.orchestration.review import ReviewError

    try:
        view = _service(db_url).inspect(permit, _jurisdiction(db_url, jurisdiction))
    except ReviewError as e:
        typer.echo(e.message, err=True)
        raise typer.Exit(1) from e
    _print_json(view) if as_json else typer.echo(format_inspect(view))


def _decide(db_url: str, permits: List[str], jurisdiction: Optional[str], decision: str, note: Optional[str],
            as_json: bool) -> None:
    from permit_trade_skills.orchestration.review import ReviewError

    reviewer = _reviewer()
    slug = _jurisdiction(db_url, jurisdiction)
    svc = _service(db_url)
    out, failed = [], 0
    for number in permits:
        try:
            out.append(svc.decide(number, slug, decision, note, reviewer=reviewer))
        except ReviewError as e:
            failed += 1
            out.append({"permit_number": number, "error": e.code, "message": e.message})
    if as_json:
        _print_json(out)
    else:
        for r in out:
            typer.echo(f"{r['permit_number']}: " + (f"{r['error']}: {r['message']}" if "error" in r else
                                                    f"{r['review_status']} by {r['reviewer']} "
                                                    f"(state {r['processing_state']})"))
    if failed:
        raise typer.Exit(1)


@cli.command("approve")
def approve(
    permit: Optional[List[str]] = typer.Option(None, "--permit", "-p", help="Permit number (repeatable)"),
    all_: bool = typer.Option(False, "--all", help="Every open SKILLS_INFERRED inference at --confidence"),
    confidence: Optional[str] = typer.Option(None, "--confidence", help="With --all: high | medium | low"),
    note: Optional[str] = typer.Option(None, "--note"),
    yes: bool = typer.Option(False, "--yes", help="With --all: approve the printed list"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Approve inferences (SKILLS_INFERRED -> SKILLS_APPROVED). Bulk: --all --confidence high --yes."""
    if bool(permit) == all_:
        typer.echo("pass --permit ... or --all --confidence <level>", err=True)
        raise typer.Exit(2)
    if not all_:
        _decide(db_url, permit or [], jurisdiction, "approve", note, as_json)
        return
    if not confidence:
        typer.echo("--all needs --confidence (high | medium | low)", err=True)
        raise typer.Exit(2)
    reviewer = _reviewer()
    slug = _jurisdiction(db_url, jurisdiction)
    svc = _service(db_url)
    rows = svc.batch_candidates(slug, confidence=confidence)
    typer.echo(f"{len(rows)} inference(s) to approve ({slug}, confidence {confidence}):")
    for r in rows:
        typer.echo(f"  {r['permit_number']}  skills={r['skills']}  review={r['review_status']}")
    if not rows:
        return
    if not yes:
        typer.echo("Nothing approved. Re-run with --yes to approve exactly this list.", err=True)
        raise typer.Exit(1)
    results = svc.decide_many([r["permit_number"] for r in rows], slug, "approve", note, reviewer=reviewer)
    if as_json:
        _print_json(results)
    errors = [r for r in results if "error" in r]
    typer.echo(f"Approved {len(results) - len(errors)} of {len(rows)} by {reviewer.name}.")
    for r in errors:
        typer.echo(f"  {r['permit_number']}: {r['error']}: {r['message']}", err=True)
    if errors:
        raise typer.Exit(1)


@cli.command("reject")
def reject(
    permit: List[str] = typer.Option(..., "--permit", "-p"),
    note: str = typer.Option(..., "--note", help="Why (required)"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Reject inferences (SKILLS_INFERRED -> SKILLS_REJECTED); a note is required."""
    _decide(db_url, permit, jurisdiction, "reject", note, as_json)


@cli.command("needs-human")
def needs_human(
    permit: List[str] = typer.Option(..., "--permit", "-p"),
    note: Optional[str] = typer.Option(None, "--note"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Flag inferences for (further) human review; the permit's state does not change."""
    _decide(db_url, permit, jurisdiction, "needs_human", note, as_json)

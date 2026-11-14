"""`pts label ...`: Stage 08 golden-set labeling (a local CLI; no web app).

Labeler names come from --labeler or PTS_LABELER. Workflow for Dataset A:
two labelers `annotate` each permit (interactively, or with flags), an
adjudicator writes the gold row with `judge`, then `export` writes
evals/golden/<dir>/trade-set.jsonl. See docs/quality_runbook.md.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="label", help="Golden-set labeling: annotate, adjudicate, export, verify")


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _service(db_url: str, jurisdiction: Optional[str]):
    from permit_trade_skills.wiring import label_service

    try:
        return label_service(db_url, jurisdiction)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _labeler(value: Optional[str]) -> str:
    name = (value or os.environ.get("PTS_LABELER") or "").strip()
    if not name:
        typer.echo("Set --labeler or PTS_LABELER", err=True)
        raise typer.Exit(2)
    return name


def _split(values: Optional[List[str]]) -> List[str]:
    return [p.strip() for v in values or [] for p in v.split(",") if p.strip()]


def _pairs(values: Optional[List[str]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for v in values or []:
        trade, _, text = v.partition("=")
        if not text:
            raise typer.BadParameter(f"expected TRADE=text, got {v!r}")
        out.setdefault(trade.strip(), []).append(text.strip())
    return out


def _fail(e: Exception) -> None:
    typer.echo(str(e), err=True)
    raise typer.Exit(1) from e


def _excerpt(content: str, width: int = 140) -> str:
    keep = []
    for line in content.splitlines():
        low = line.lower()
        if low.startswith(("permit:", "status:", "address:", "description:", "permit_type", "classification",
                           "est_project_cost")) or any(k in low for k in (
                               "project description", "mechanical", "hvac", "where on your property",
                               "envelope", "earth disturbance", "fire", "plumb", "electric", "roof", "demol")):
            keep.append(line[:width])
    return "\n".join(keep)


@cli.command("list")
def list_command(
    kind: str = typer.Option("trade_set", "--kind", "-k"),
    candidates: bool = typer.Option(False, "--candidates", help="Also list permits with a snapshot and no label"),
    limit: int = typer.Option(100, "--limit", "-n"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Label status per item (labelers, adjudicator); --candidates adds unlabeled permits with snapshots."""
    svc = _service(db_url, jurisdiction)
    extra = []
    if candidates:
        extra = [s["permit_number"] for s in svc.store.recent_snapshots(svc.jurisdiction, limit=limit)]
    rows = svc.queue(kind, extra)
    if as_json:
        _print_json(rows)
        return
    typer.echo(f"{'item':<14} {'labelers':<40} adjudicated_by")
    for r in rows:
        typer.echo(f"{r['item_key']:<14} {', '.join(r['labelers']) or '-':<40} {r['adjudicated_by'] or '-'}")
    done = sum(1 for r in rows if r["adjudicated_by"])
    typer.echo(f"\n{len(rows)} item(s), {done} adjudicated")


@cli.command("show")
def show_command(
    permit: str = typer.Option(..., "--permit", "-p"),
    kind: str = typer.Option("trade_set", "--kind", "-k"),
    full: bool = typer.Option(False, "--full", help="Print the whole detail text"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """The permit's labeling input (detail excerpt) and its current labels."""
    from permit_trade_skills.eval.labels import LabelError

    svc = _service(db_url, jurisdiction)
    try:
        row, inp, pinned = svc.permit_input("trade_set", permit)
    except LabelError as e:
        _fail(e)
    snap = inp["snapshot"]
    typer.echo(f"{permit}  {row['status']}  {row.get('permit_class_mapped')} / {row.get('permit_type_desc')}  "
               f"snapshot #{snap['id']} {snap['fetch_strategy']}{' [degraded]' if snap['degraded'] else ''}")
    typer.echo(f"pinned_input_hash {pinned[:16]}…\n")
    typer.echo(snap["content_text"] if full else _excerpt(snap["content_text"]))
    for r in svc.store.current_labels(svc.jurisdiction, kind, permit):
        typer.echo(f"\n[{r['role']}] {r['labeled_by']} v{r['label_version']} "
                   f"{'(current input)' if r['pinned_input_hash'] == pinned else '(OTHER INPUT)'}")
        typer.echo(json.dumps(r["payload"], ensure_ascii=False))


def _ask_trades(prompt: str, names: List[str], default: str = "") -> List[str]:
    raw = typer.prompt(prompt, default=default, show_default=bool(default))
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(names[int(part) - 1] if part.isdigit() and 0 < int(part) <= len(names) else part)
    return out


@cli.command("annotate")
def annotate_command(
    permit: str = typer.Option(..., "--permit", "-p"),
    labeler: Optional[str] = typer.Option(None, "--labeler", help="Default PTS_LABELER"),
    required: Optional[List[str]] = typer.Option(None, "--required", help="Required trades (comma/repeatable)"),
    optional: Optional[List[str]] = typer.Option(None, "--optional", help="Defensible extra trades"),
    unrequired: Optional[List[str]] = typer.Option(None, "--unrequired", help="Precision traps"),
    skill: Optional[List[str]] = typer.Option(None, "--skill", help="TRADE=capability phrase (repeatable)"),
    difficulty: Optional[str] = typer.Option(None, "--difficulty", help="easy | medium | hard"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Label a permit's trade set (Dataset A). Without --required it asks interactively."""
    from permit_trade_skills.eval import trades as TR
    from permit_trade_skills.eval.labels import TRADE_SET, LabelError

    name = _labeler(labeler)
    svc = _service(db_url, jurisdiction)
    if required is None:
        try:
            row, inp, _ = svc.permit_input(TRADE_SET, permit)
        except LabelError as e:
            _fail(e)
        typer.echo(_excerpt(inp["snapshot"]["content_text"]))
        names = list(TR.TRADES)
        typer.echo("\nTrades: " + "  ".join(f"{i}. {n}" for i, n in enumerate(names, 1)))
        req = _ask_trades("Required trades (numbers or names, comma-separated)", names)
        opt = _ask_trades("Optional (defensible extra) trades", names, default=" ")
        trap = _ask_trades("Unrequired trades a naive model might claim (traps)", names, default=" ")
        skills = {}
        for t in req:
            phrase = typer.prompt(f"Defensible skill for {t} (blank to skip)", default="", show_default=False)
            if phrase.strip():
                skills[t] = [phrase.strip()]
        diff = typer.prompt("Difficulty (easy/medium/hard)", default="medium")
        note = typer.prompt("Notes", default="", show_default=False)
    else:
        req, opt, trap, skills, diff, note = (_split(required), _split(optional), _split(unrequired),
                                              _pairs(skill), difficulty or "medium", notes or "")
    try:
        out = svc.annotate(TRADE_SET, permit, {"required_trades": req, "optional_trades": opt,
                                               "unrequired_trades": trap, "defensible_skills": skills,
                                               "difficulty": diff, "notes": note}, name)
    except LabelError as e:
        _fail(e)
    typer.echo(f"{permit}: labeled by {name} (v{out['label_version']}): required {out['payload']['required_trades']}")


@cli.command("judge")
def adjudicate_command(
    permit: str = typer.Option(..., "--permit", "-p"),
    adjudicator: Optional[str] = typer.Option(None, "--adjudicator", "--labeler",
                                              help="Adjudicator name (alias --labeler); default PTS_LABELER"),
    note: str = typer.Option(..., "--note", help="Adjudication note (stored)"),
    drop: Optional[List[str]] = typer.Option(None, "--drop", help="TRADE=why it is not required (repeatable)"),
    single_labeler: bool = typer.Option(False, "--single-labeler", help="Phase 1: one labeler + adjudicator"),
    difficulty: Optional[str] = typer.Option(None, "--difficulty"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Adjudicate a permit's labels into the gold trade set (union of required trades, minus noted drops)."""
    from permit_trade_skills.eval.labels import LabelError

    svc = _service(db_url, jurisdiction)
    drops = {k: " / ".join(v) for k, v in _pairs(drop).items()}
    try:
        out = svc.adjudicate(permit, _labeler(adjudicator), note, drop=drops, single_labeler=single_labeler,
                             difficulty=difficulty)
    except LabelError as e:
        _fail(e)
    p = out["payload"]
    typer.echo(f"{permit}: gold required {p['required_trades']}; disagreements {p['disagreements'] or 'none'}; "
               f"dropped {[d['trade'] for d in p['dropped']] or 'none'}")


@cli.command("checklist")
def checklist_command(
    permit: str = typer.Option(..., "--permit", "-p"),
    line: List[str] = typer.Option(..., "--line", help="A must-appear substring (3-5, repeatable)"),
    labeler: Optional[str] = typer.Option(None, "--labeler"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Dataset C: the lines that must appear verbatim in the permit's detail text."""
    from permit_trade_skills.eval.labels import CHECKLIST, LabelError

    svc = _service(db_url, jurisdiction)
    try:
        out = svc.annotate(CHECKLIST, permit, {"must_appear": line, "notes": notes or ""}, _labeler(labeler))
    except LabelError as e:
        _fail(e)
    typer.echo(f"{permit}: checklist v{out['label_version']} ({len(line)} lines)")


@cli.command("nl")
def nl_command(
    file: Path = typer.Option(..., "--file", "-f", help="JSONL rows {id, question, expected, expected_unfulfilled?}"),
    as_of: Optional[str] = typer.Option(None, "--as-of", help="Date tokens resolve against this day (default today)"),
    labeler: Optional[str] = typer.Option(None, "--labeler"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Dataset B: pin NL questions (gold row count of the expected query at label time)."""
    from permit_trade_skills.eval.labels import NL_QUERY, LabelError, read_jsonl

    svc = _service(db_url, jurisdiction)
    name = _labeler(labeler)
    day = as_of or date.today().isoformat()
    for row in read_jsonl(file):
        payload = {**row, "as_of": row.get("as_of") or day}
        try:
            out = svc.annotate(NL_QUERY, row["id"], payload, name)
        except LabelError as e:
            typer.echo(f"{row.get('id')}: {e}", err=True)
            continue
        typer.echo(f"{row['id']}: gold_total {out['payload']['gold_total']} (as of {payload['as_of']})")


@cli.command("import")
def import_command(
    file: Path = typer.Argument(..., help="JSONL label rows (e.g. drafts)"),
    labeler: Optional[str] = typer.Option(None, "--labeler", help="Override each row's labeled_by"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Import label rows {kind, permit_number|item_key, payload, labeled_by} (each validated and hashed)."""
    from permit_trade_skills.eval.labels import read_jsonl

    svc = _service(db_url, jurisdiction)
    out = svc.import_rows(read_jsonl(file), labeled_by=labeler)
    typer.echo(f"imported {out['imported']} label(s)")
    for e in out["errors"]:
        typer.echo(f"  {e['item_key']}: {e['error']}", err=True)
    raise typer.Exit(1 if out["errors"] else 0)


@cli.command("export")
def export_command(
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help="trade_set | nl_query | extraction_checklist "
                                                                  "(default: all three)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    provisional: bool = typer.Option(False, "--provisional",
                                     help="trade_set: export unadjudicated labeler rows to "
                                          "<dataset>/labels/provisional/ for a baseline (never the gold file)"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    db_url: str = db_url_option(),
) -> None:
    """Write the golden JSONL files (adjudicated trade sets; newest NL and checklist labels)."""
    from permit_trade_skills.eval.config import DATASET_FILES, dataset_dir

    svc = _service(db_url, jurisdiction)
    if provisional:
        path = output or dataset_dir(svc.jurisdiction) / "labels" / "provisional" / DATASET_FILES["trade_set"]
        out = svc.export("trade_set", path, provisional=True)
        typer.echo(f"trade_set (PROVISIONAL, not adjudicated): {out['written']} row(s) -> {out['path']}")
        typer.echo(f"  run: pts eval run -s H2 --golden-dir {path.parent}")
        return
    for k in [kind] if kind else list(DATASET_FILES):
        path = output if (output and kind) else dataset_dir(svc.jurisdiction) / DATASET_FILES[k]
        out = svc.export(k, path)
        typer.echo(f"{k}: {out['written']} row(s) -> {out['path']}")
        for s in out["skipped"]:
            typer.echo(f"  skipped {s['item_key']}: {s['reason']}", err=True)


@cli.command("verify")
def verify_command(
    golden_dir: Optional[Path] = typer.Option(None, "--golden-dir"),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j"),
    as_json: bool = typer.Option(False, "--json"),
    db_url: str = db_url_option(),
) -> None:
    """Re-hash every golden row against its pinned input; SUPERSEDED rows are not scored. Exit 1 if any."""
    from permit_trade_skills.eval.config import DATASET_FILES, dataset_dir
    from permit_trade_skills.eval.labels import ACTIVE, load_golden

    svc = _service(db_url, jurisdiction)
    base = golden_dir or dataset_dir(svc.jurisdiction)
    report: Dict[str, Any] = {}
    for kind, fname in DATASET_FILES.items():
        rows = load_golden(base / fname, kind, store=svc.store, jurisdiction=svc.jurisdiction)
        report[kind] = {"file": str(base / fname), "rows": len(rows),
                        "active": sum(r["status"] == ACTIVE for r in rows),
                        "superseded": [{"id": r.get("id"), "reason": r["status_reason"]}
                                       for r in rows if r["status"] != ACTIVE]}
    if as_json:
        _print_json(report)
    else:
        for kind, r in report.items():
            typer.echo(f"{kind:<22} {r['active']}/{r['rows']} ACTIVE  ({r['file']})")
            for s in r["superseded"]:
                typer.echo(f"  SUPERSEDED {s['id']}: {s['reason']}")
    raise typer.Exit(1 if any(r["superseded"] for r in report.values()) else 0)

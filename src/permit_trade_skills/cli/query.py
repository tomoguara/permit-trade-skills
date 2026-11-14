"""`pts query "<question>"` (natural-language query) and `pts api serve|openapi`."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

import typer

from permit_trade_skills.config import db_url_option

api_cli = typer.Typer(name="api", help="Run the query REST API or export its OpenAPI spec")


def query_command(
    question: str = typer.Argument(..., help='e.g. "issued residential permits over $50k in the last 90 days"'),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", "-j",
                                               help="Jurisdiction slug (default: the one named in the "
                                                    "question, or the only registered one)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full API response as JSON"),
    page_size: Optional[int] = typer.Option(None, "--page-size", help="Rows per page (default 25, max 200)"),
    cursor: Optional[str] = typer.Option(None, "--cursor", help="next_cursor from a previous run"),
    db_url: str = db_url_option(),
) -> None:
    """Answer a natural-language permit question (same service as POST /api/v1/query)."""
    from permit_trade_skills.query.errors import QueryError
    from permit_trade_skills.query.service import QueryService, current_trace_id

    trace_id = uuid.uuid4().hex
    current_trace_id.set(trace_id)
    try:
        result = QueryService(db_url).ask(question, jurisdiction, page_size=page_size, cursor=cursor)
    except QueryError as e:
        problem = {"error_code": e.error_code, "message": e.message, "status": e.status,
                   "retryable": e.retryable, "trace_id": trace_id, **e.extra}
        typer.echo(json.dumps(problem, indent=2) if as_json else f"Error {e.error_code}: {e.message}", err=True)
        raise typer.Exit(code=2 if e.status == 503 else 1) from e
    result["trace_id"] = trace_id

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(result["echo"])
    typer.echo(f"Query: {json.dumps(result['query'])}")
    for label, key in (("Assumptions", "assumptions"), ("Ambiguous", "ambiguous_terms"),
                       ("Unfulfilled filters", "unfulfilled_filters")):
        if result[key]:
            typer.echo(f"{label}: " + "; ".join(result[key]))
    if result["clarification"]:
        tag = " (LLM-generated)" if result["clarification"]["llm_generated"] else ""
        typer.echo(f"Clarification{tag}: {result['clarification']['text']}")
    coverage = result.get("trade_coverage")
    total = f"{result['total_estimate']:,}" if coverage else f"~{result['total_estimate']:,} (estimate)"
    typer.echo(f"Rows: {result['count']} of {total}"
               + (" [cached parse]" if result["nlu"]["cached"] else ""))
    if coverage:
        typer.echo(f"Trade coverage: {coverage['inferred']:,} of {coverage['candidates']:,} candidate "
                   "permits have a skill inference")
        if coverage["note"]:
            typer.echo(f"  Note: {coverage['note']}")
    # Trade queries: a column with each requested trade's confidence in the permit's inference.
    trades = {r["permit_number"]: " ".join(f"{t}:{c}" for t, c in (r.get("trade_confidence") or {}).items())
              for r in result["rows"]} if coverage else {}
    width = max(map(len, trades.values()), default=0)
    for r in result["rows"]:
        cost = f"${r['valuation']:,.0f}" if r["valuation"] is not None else "n/a"
        issued = (r["issued_at"] or "")[:10] or "not issued"
        trade_col = f"{trades[r['permit_number']]:<{width}}  " if width else ""
        typer.echo(f"  {r['permit_number']:<14} {r['status']:<13} {issued:<11} {cost:>12}  "
                   f"{trade_col}{(r['description'] or '')[:70]}")
    if result["next_cursor"]:
        typer.echo(f"More: --cursor {result['next_cursor']}")
    typer.echo(f"trace_id: {trace_id}")


@api_cli.command(name="serve")
def serve_command(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    db_url: str = db_url_option(),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)"),
) -> None:
    """Run the REST API with uvicorn (docs at http://HOST:PORT/docs)."""
    import logging
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.environ["PERMIT_TRADE_SKILLS_DB_URL"] = db_url
    uvicorn.run("permit_trade_skills.server:create_app", factory=True, host=host, port=port, reload=reload)


@api_cli.command(name="openapi")
def openapi_command(
    output: Path = typer.Option(Path("docs/api/openapi.yaml"), "--output", "-o", help="Output file (.yaml or .json)"),
) -> None:
    """Write the OpenAPI document (no database needed)."""
    import yaml

    from permit_trade_skills.server import create_app

    spec = create_app(service=object()).openapi()  # type: ignore[arg-type]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix == ".json":
        output.write_text(json.dumps(spec, indent=2) + "\n")
    else:
        output.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    typer.echo(f"Wrote {output}")

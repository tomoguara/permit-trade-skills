"""`pts permit show`: one permit as JSON (same service as GET /api/v1/permits/{n})."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, Optional

import typer

from permit_trade_skills.config import db_url_option

cli = typer.Typer(name="permit", help="Look up a single permit")

DEFAULT_JURISDICTION = "seattle-kingcounty-wa"


def _fail(problem: Dict[str, Any], code: int = 1) -> None:
    typer.echo(json.dumps(problem, indent=2), err=True)
    raise typer.Exit(code=code)


def _source_row(db_url: str, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
    """Fetch the permit's current row from the jurisdiction's Socrata dataset (live, not stored)."""
    from permit_trade_skills.discovery.registry import Registry
    from permit_trade_skills.ingest.soda_client import SodaClient, soda_resource_base

    registry = Registry(database_url=db_url)
    session = registry.get_session()
    try:
        j = registry.get_jurisdiction(session, jurisdiction)
        if j is None:
            raise LookupError(f"Jurisdiction {jurisdiction!r} is not registered.")
        base_url, dataset_id = j.base_url, j.dataset_id
        field_map = j.field_map if isinstance(j.field_map, dict) else json.loads(j.field_map or "{}")
    finally:
        session.close()
    base = soda_resource_base(base_url, dataset_id)
    if not base:
        raise LookupError(f"Jurisdiction {jurisdiction!r} has no base_url/dataset_id for a row lookup.")
    key_column = field_map.get("permit_num", "permit_num")

    async def fetch():
        async with SodaClient(base_url=base) as client:
            return await client.get_row_by_key(key_column, permit_number, select=":*,*")

    return asyncio.run(fetch())


@cli.command("show")
def show(
    permit_number: str = typer.Argument(..., help="Permit number, e.g. 7156276-DM"),
    jurisdiction: str = typer.Option(DEFAULT_JURISDICTION, "--jurisdiction", "-j", help="Jurisdiction slug"),
    raw: bool = typer.Option(False, "--raw",
                             help="Print the source open-data row instead, fetched live from the "
                                  "jurisdiction's API (the stored record keeps only mapped fields)"),
    db_url: str = db_url_option(),
) -> None:
    """Print one permit as JSON: the stored record, status history, detail snapshot and skill inference."""
    if raw:
        from permit_trade_skills.ingest.soda_client import SourceUnavailable

        try:
            row = _source_row(db_url, jurisdiction, permit_number.strip())
        except (LookupError, SourceUnavailable) as e:
            _fail({"error_code": "SOURCE_LOOKUP_FAILED", "message": str(e)})
        if row is None:
            _fail({"error_code": "PERMIT_NOT_FOUND",
                   "message": f"Permit {permit_number!r} not found in the {jurisdiction} source dataset."})
        typer.echo(json.dumps(row, indent=2))
        return

    from permit_trade_skills.query.errors import QueryError
    from permit_trade_skills.query.service import QueryService, current_trace_id

    trace_id = uuid.uuid4().hex
    current_trace_id.set(trace_id)
    try:
        result = QueryService(db_url).get_permit(jurisdiction, permit_number)
    except QueryError as e:
        _fail({"error_code": e.error_code, "message": e.message, "status": e.status,
               "retryable": e.retryable, "trace_id": trace_id, **e.extra},
              code=2 if e.status == 503 else 1)
    result["trace_id"] = trace_id
    typer.echo(json.dumps(result, indent=2, default=str))

"""CLI commands for hybrid search and semantic similarity."""

import typer
from permit_trade_skills.config import db_url_option
from typing import Optional, List
from enum import Enum

from permit_trade_skills.discovery.registry import Registry


cli = typer.Typer(
    name="search",
    help="Search permits using hybrid search (structured + text + vector)",
)


STRUCTURED_KEYS = ("status", "permit_class", "min_cost", "max_cost", "issued_after")
STRUCTURED_HELP = (
    "Structured filters as JSON; every result must match all of them. "
    "Keys: status, permit_class (string or list), min_cost, max_cost, issued_after. "
    """Example: '{"status": "ISSUED", "min_cost": 10000}'"""
)


def parse_structured(structured: Optional[str]) -> dict:
    """Parse and validate a --structured JSON string. Exits with code 1 on bad input."""
    import json

    if not structured:
        return {}
    try:
        filters = json.loads(structured)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON in --structured: {e}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(filters, dict):
        typer.echo("--structured must be a JSON object, e.g. '{\"status\": \"ISSUED\"}'", err=True)
        raise typer.Exit(code=1)
    unknown = sorted(set(filters) - set(STRUCTURED_KEYS))
    if unknown:
        typer.echo(
            f"Unknown --structured key(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(STRUCTURED_KEYS)}",
            err=True,
        )
        raise typer.Exit(code=1)
    for key in ("min_cost", "max_cost"):
        if key in filters and (isinstance(filters[key], bool) or not isinstance(filters[key], (int, float))):
            typer.echo(f"--structured {key} must be a number, got {filters[key]!r}", err=True)
            raise typer.Exit(code=1)
    if "issued_after" in filters:
        try:
            _parse_date(filters["issued_after"])
        except (TypeError, ValueError):
            typer.echo(
                f"--structured issued_after must be an ISO date like \"2024-01-01\", "
                f"got {filters['issued_after']!r}",
                err=True,
            )
            raise typer.Exit(code=1)
    return filters


def _parse_date(value):
    """Parse an ISO date or datetime string into a datetime."""
    from datetime import datetime

    if not isinstance(value, str):
        raise TypeError("expected a string")
    return datetime.fromisoformat(value)


def _format_address(permit) -> str:
    """'550 ALOHA ST, SEATTLE, WA 98109', skipping any missing parts (no 'None')."""
    state_zip = " ".join(str(v) for v in (permit.state, permit.zip_code) if v)
    parts = [str(v) for v in (permit.address, permit.city) if v]
    if state_zip:
        parts.append(state_zip)
    return ", ".join(parts) or "N/A"


def _preview(text: Optional[str], limit: int) -> str:
    """Truncate text to `limit` chars, adding '...' only when truncated."""
    if not text:
        return "N/A"
    return text[:limit] + "..." if len(text) > limit else text


def _echo_permit_link(permit) -> None:
    """Print the permit's portal page link, if it has one."""
    if permit.detail_url:
        typer.echo(f"   Permit link: {permit.detail_url}")


class SearchMode(str, Enum):
    STRUCTURED = "structured"
    TEXT = "text"
    VECTOR = "vector"
    HYBRID = "hybrid"


@cli.command(name="hybrid")
def hybrid_search_command(
    query: str = typer.Argument(..., help="Search query"),
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug"),
    db_url: str = db_url_option(),
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum number of results"),
    structured: Optional[str] = typer.Option(None, "--structured", help=STRUCTURED_HELP),
) -> None:
    """
    Hybrid search combining structured filters, full-text search, and semantic similarity.

    Structured filters (--structured JSON) narrow the candidates; keyword
    (trigram) and vector rankings over that set are fused with RRF
    (Reciprocal Rank Fusion). Filter keys: status, permit_class, min_cost,
    max_cost, issued_after. status/permit_class accept a string or a list.
    """
    import asyncio
    import json

    async def run():
        registry = Registry(database_url=db_url)
        session = registry.get_session()

        try:
            # Get jurisdiction
            jurisdiction_obj = registry.get_jurisdiction(session, jurisdiction)
            if not jurisdiction_obj:
                typer.echo(f"Jurisdiction '{jurisdiction}' not found")
                return

            filters = parse_structured(structured)

            # Perform hybrid search
            results, search_metadata = hybrid_search(
                session,
                jurisdiction_obj.id,
                query,
                limit=limit,
                filters=filters
            )

            # Display results
            typer.echo(f"Found {len(results)} results for '{query}'")
            typer.echo(f"Search metadata: {json.dumps(search_metadata, indent=2)}")
            typer.echo("")

            if results:
                typer.echo("Results:")
                typer.echo("--------")
                for i, (permit, score, reasons) in enumerate(results, 1):
                    typer.echo(f"\n{i}. {permit.permit_number} - {permit.status}")
                    typer.echo(f"   Type: {permit.permit_type_mapped} - {permit.permit_type_desc or 'N/A'}")
                    typer.echo(f"   Class: {permit.permit_class_mapped or 'N/A'}")
                    typer.echo(f"   Cost: ${permit.valuation:,.2f}" if permit.valuation else "   Cost: N/A")
                    typer.echo(f"   Address: {_format_address(permit)}")
                    typer.echo(f"   Issued: {permit.issued_at.strftime('%Y-%m-%d') if permit.issued_at else 'N/A'}")
                    typer.echo(f"   Similarity score: {score:.3f}")
                    typer.echo(f"   Match reasons: {', '.join(reasons)}")
                    if permit.description:
                        desc_preview = permit.description[:200] + "..." if len(permit.description) > 200 else permit.description
                        typer.echo(f"   Description: {desc_preview}")
                    _echo_permit_link(permit)

        finally:
            session.close()

    asyncio.run(run())


@cli.command(name="vector")
def vector_search_command(
    query: str = typer.Argument(..., help="Search query"),
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug"),
    db_url: str = db_url_option(),
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum number of results"),
    structured: Optional[str] = typer.Option(None, "--structured", help=STRUCTURED_HELP),
) -> None:
    """
    Semantic vector search using embeddings.

    Finds permits with descriptions semantically similar to the query.
    """
    import asyncio
    import json

    async def run():
        registry = Registry(database_url=db_url)
        session = registry.get_session()

        try:
            jurisdiction_obj = registry.get_jurisdiction(session, jurisdiction)
            if not jurisdiction_obj:
                typer.echo(f"Jurisdiction '{jurisdiction}' not found")
                return

            filters = parse_structured(structured)
            results = vector_search(session, jurisdiction_obj.id, query, limit=limit, filters=filters)

            typer.echo(f"Found {len(results)} vector search results for '{query}'")
            if filters:
                typer.echo(f"Filters: {filters}")

            if results:
                typer.echo("\nResults:")
                typer.echo("--------")
                for i, (permit, score) in enumerate(results, 1):
                    typer.echo(f"\n{i}. {permit.permit_number} - {score:.3f}")
                    typer.echo(f"   {_preview(permit.description, 150)}")
                    _echo_permit_link(permit)

        finally:
            session.close()

    asyncio.run(run())


@cli.command(name="text")
def text_search_command(
    query: str = typer.Argument(..., help="Search query"),
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug"),
    db_url: str = db_url_option(),
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum number of results"),
    structured: Optional[str] = typer.Option(None, "--structured", help=STRUCTURED_HELP),
) -> None:
    """
    Full-text search using PostgreSQL trigram similarity.

    Finds permits with text similarity to the query.
    """
    import asyncio

    async def run():
        registry = Registry(database_url=db_url)
        session = registry.get_session()

        try:
            jurisdiction_obj = registry.get_jurisdiction(session, jurisdiction)
            if not jurisdiction_obj:
                typer.echo(f"Jurisdiction '{jurisdiction}' not found")
                return

            filters = parse_structured(structured)
            results = text_search(session, jurisdiction_obj.id, query, limit=limit, filters=filters)

            typer.echo(f"Found {len(results)} text search results for '{query}'")
            if filters:
                typer.echo(f"Filters: {filters}")

            if results:
                typer.echo("\nResults:")
                typer.echo("--------")
                for i, (permit, score) in enumerate(results, 1):
                    typer.echo(f"\n{i}. {permit.permit_number} - similarity: {score:.3f}")
                    typer.echo(f"   {permit.permit_type_mapped or 'N/A'} - {_preview(permit.description, 100)}")
                    _echo_permit_link(permit)

        finally:
            session.close()

    asyncio.run(run())


@cli.command(name="similar")
def similar_command(
    permit_id: str = typer.Option(
        ..., "--permit-id", "-p",
        help="Permit number (e.g. 7140363-RR) or internal numeric row id of the permit to compare against",
    ),
    jurisdiction: str = typer.Option(..., "--jurisdiction", "-j", help="Jurisdiction slug"),
    db_url: str = db_url_option(),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of similar permits to return"),
    structured: Optional[str] = typer.Option(None, "--structured", help=STRUCTURED_HELP),
) -> None:
    """
    Find permits similar to a given permit using vector similarity.

    Uses cosine similarity on description embeddings to find
    permits with similar project descriptions.
    """
    import asyncio

    async def run():
        registry = Registry(database_url=db_url)
        session = registry.get_session()

        try:
            jurisdiction_obj = registry.get_jurisdiction(session, jurisdiction)
            if not jurisdiction_obj:
                typer.echo(f"Jurisdiction '{jurisdiction}' not found")
                return

            filters = parse_structured(structured)
            source = resolve_permit(session, jurisdiction_obj.id, permit_id)
            if source is None:
                typer.echo(f"Permit '{permit_id}' not found in {jurisdiction_obj.id}")
                raise typer.Exit(code=1)
            if source.description_embedding is None:
                typer.echo(f"Permit {source.permit_number} has no description embedding (no description)")
                raise typer.Exit(code=1)

            results = find_similar_permits(
                session, jurisdiction_obj.id, source.id, limit=limit, filters=filters
            )

            if not results:
                suffix = f" matching {filters}" if filters else ""
                typer.echo(f"No similar permits found for permit {source.permit_number}{suffix}")
                return

            typer.echo(f"Found {len(results)} permits similar to permit {source.permit_number}")
            if filters:
                typer.echo(f"Filters: {filters}")
            if source.description:
                typer.echo(f"Source description: {source.description[:150]}")

            for i, (permit, score) in enumerate(results, 1):
                typer.echo(f"\n{i}. {permit.permit_number} - similarity: {score:.3f}")
                typer.echo(f"   Type: {permit.permit_type_mapped} - {permit.permit_type_desc or 'N/A'}")
                typer.echo(f"   Description: {_preview(permit.description, 150)}")
                _echo_permit_link(permit)

        finally:
            session.close()

    asyncio.run(run())


# =============================================================================
# Search Functions (Database-level)
# =============================================================================


def _as_list(value) -> list:
    """Wrap a scalar filter value in a list; pass lists/tuples through."""
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _structured_conditions(filters: Optional[dict]) -> list:
    """Translate a --structured JSON dict into SQLAlchemy filter conditions.

    Supported keys: status (str or list), permit_class (str or list),
    min_cost, max_cost, issued_after. Unknown keys are ignored.
    """
    from sqlalchemy import func, or_
    from permit_trade_skills.discovery.registry import PermitTable

    if not filters:
        return []
    conditions = []
    if filters.get("status"):
        # Accept "ISSUED" or ["ISSUED", "COMPLETED"]; canonical statuses are upper-case.
        statuses = [str(v).upper() for v in _as_list(filters["status"])]
        conditions.append(PermitTable.status.in_(statuses))
    if filters.get("permit_class"):
        # Case-insensitive match against either the raw class ("Commercial",
        # "Single Family/Duplex") or the mapped class ("Residential",
        # "Non-Residential"), so "RESIDENTIAL" and "COMMERCIAL" both work.
        classes = [str(v).lower() for v in _as_list(filters["permit_class"])]
        conditions.append(or_(
            func.lower(PermitTable.permit_class).in_(classes),
            func.lower(PermitTable.permit_class_mapped).in_(classes),
        ))
    if filters.get("min_cost") is not None:
        conditions.append(PermitTable.valuation >= filters["min_cost"])
    if filters.get("max_cost") is not None:
        conditions.append(PermitTable.valuation <= filters["max_cost"])
    if filters.get("issued_after"):
        conditions.append(PermitTable.issued_at >= _parse_date(filters["issued_after"]))
    return conditions


def _vector_ranked(session, jurisdiction_id: str, embedding, limit: int,
                   exclude_id: Optional[int] = None, conditions: Optional[list] = None):
    """Return [(permit, cosine_similarity)] ordered by similarity, computed in SQL."""
    from permit_trade_skills.discovery.registry import PermitTable

    distance = PermitTable.description_embedding.cosine_distance(embedding)
    q = session.query(PermitTable, distance.label("distance")).filter(
        PermitTable.jurisdiction_id == jurisdiction_id,
        PermitTable.description_embedding.isnot(None),
        *(conditions or []),
    )
    if exclude_id is not None:
        q = q.filter(PermitTable.id != exclude_id)
    rows = q.order_by(distance).limit(limit).all()
    return [(permit, 1.0 - float(dist)) for permit, dist in rows]


def _text_ranked(session, jurisdiction_id: str, query: str, limit: int,
                 conditions: Optional[list] = None):
    """Return [(permit, trigram_word_similarity)] for descriptions containing the query.

    Requires the pg_trgm extension (created by Registry.create_tables()).
    """
    from sqlalchemy import func
    from permit_trade_skills.discovery.registry import PermitTable

    similarity = func.word_similarity(query, PermitTable.description)
    rows = session.query(PermitTable, similarity.label("similarity")).filter(
        PermitTable.jurisdiction_id == jurisdiction_id,
        PermitTable.description.ilike(f"%{query}%"),
        *(conditions or []),
    ).order_by(similarity.desc(), PermitTable.id).limit(limit).all()
    return [(permit, float(sim)) for permit, sim in rows]


def hybrid_search(
    session,
    jurisdiction_id: str,
    query: str,
    limit: int = 25,
    filters: Optional[dict] = None
):
    """
    Hybrid search: structured filters narrow the candidates; text (trigram) and
    vector (semantic) rankings over the filtered set are fused with
    Reciprocal Rank Fusion (RRF, k=60).

    Every result satisfies all structured filters.
    """
    from permit_trade_skills.embeddings import compute_embedding

    conditions = _structured_conditions(filters)

    # 1. Text search (trigram similarity) within the filtered set
    text_results = _text_ranked(session, jurisdiction_id, query, limit * 2, conditions)

    # 2. Vector search (semantic similarity) within the filtered set
    vector_results = []
    try:
        query_embedding = compute_embedding(query)
        if query_embedding:
            vector_results = _vector_ranked(
                session, jurisdiction_id, query_embedding, limit * 2, conditions=conditions
            )
    except Exception as e:
        session.rollback()
        typer.echo(f"Vector search unavailable: {e}")

    # 3. Fuse with RRF (k=60)
    all_results = {}
    for reason, ranked in (("text", text_results), ("vector", vector_results)):
        for rank, (permit, _) in enumerate(ranked, 1):
            entry = all_results.setdefault(permit.id, {"permit": permit, "score": 0.0, "reasons": []})
            entry["score"] += 1.0 / (60 + rank)
            entry["reasons"].append(reason)

    sorted_results = sorted(all_results.values(), key=lambda d: d["score"], reverse=True)[:limit]
    results = [(d["permit"], d["score"], d["reasons"]) for d in sorted_results]

    search_metadata = {
        "modes": ["text", "vector"],
        "filters": filters or {},
        "fusion": "RRF",
        "total_candidates": len(all_results),
        "text_matches": len(text_results),
        "vector_matches": len(vector_results),
    }

    return results, search_metadata


def vector_search(session, jurisdiction_id: str, query: str, limit: int = 25,
                  filters: Optional[dict] = None):
    """Find permits with semantically similar descriptions (optionally filtered)."""
    from permit_trade_skills.embeddings import compute_embedding

    query_embedding = compute_embedding(query)
    if not query_embedding:
        return []
    return _vector_ranked(
        session, jurisdiction_id, query_embedding, limit, conditions=_structured_conditions(filters)
    )


def text_search(session, jurisdiction_id: str, query: str, limit: int = 25,
                filters: Optional[dict] = None):
    """Find permits with text similarity to query (optionally filtered)."""
    return _text_ranked(session, jurisdiction_id, query, limit, _structured_conditions(filters))


def resolve_permit(session, jurisdiction_id: str, permit_ref: str):
    """Find a permit by permit number, or by internal row id if numeric."""
    from permit_trade_skills.discovery.registry import PermitTable

    ref = permit_ref.strip()
    permit = session.query(PermitTable).filter(
        PermitTable.jurisdiction_id == jurisdiction_id,
        PermitTable.permit_number == ref,
    ).first()
    if permit is None and ref.isdigit():
        permit = session.query(PermitTable).filter(
            PermitTable.jurisdiction_id == jurisdiction_id,
            PermitTable.id == int(ref),
        ).first()
    return permit


def find_similar_permits(session, jurisdiction_id: str, permit_id: int, limit: int = 10,
                         filters: Optional[dict] = None):
    """Find permits similar to a given permit using vector similarity.

    Filters apply to the candidate permits, not to the source permit.
    """
    from permit_trade_skills.discovery.registry import PermitTable

    source_permit = session.query(PermitTable).filter(
        PermitTable.jurisdiction_id == jurisdiction_id,
        PermitTable.id == permit_id,
        PermitTable.description_embedding.isnot(None)
    ).first()

    # The loaded embedding is a numpy array; test for None, not truthiness.
    if source_permit is None or source_permit.description_embedding is None:
        return []

    return _vector_ranked(
        session, jurisdiction_id, source_permit.description_embedding, limit,
        exclude_id=permit_id, conditions=_structured_conditions(filters),
    )

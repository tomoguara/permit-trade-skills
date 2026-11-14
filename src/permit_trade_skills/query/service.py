"""QueryService: the one implementation behind the REST API, the tools and `pts query`.

All reads go through the store repositories (`permit_trade_skills.store`).
Errors are raised as `QueryError`; the API renders them as problem+json and
the tools return them as `{ok: false, ...}`.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from permit_trade_skills.query import errors as E
from permit_trade_skills.query.echo import render_echo
from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.nlu import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    NLUParser,
    interpret,
    match_jurisdictions,
)
from permit_trade_skills.query.params import (
    STATUSES,
    check_window,
    parse_date,
    parse_params,
)
from permit_trade_skills.query.retrieve import DEFAULT_MODES, RetrievalResult, retrieve
from permit_trade_skills.skills.schemas import TRADES
from permit_trade_skills.store import session_factory
from permit_trade_skills.store.query import PermitQuery
from permit_trade_skills.store.repos import (
    InferenceRepository,
    JurisdictionRepository,
    NluCacheRepository,
    PermitRepository,
    SnapshotRepository,
)

log = logging.getLogger("pts.query")

# Set per request by the API middleware (and by the CLI); included in logs and responses.
current_trace_id: ContextVar[Optional[str]] = ContextVar("pts_trace_id", default=None)

DISPLAY_STATUSES = [s for s in STATUSES if s != "UNKNOWN"]  # UNKNOWN shown only when > 0
DATE_FIELDS = ("applied_at", "issued_at")
KNOWN_VALUES_TTL_S = 600

ROW_FIELDS = ("permit_number", "status_raw", "status", "permit_class", "permit_class_mapped",
              "permit_type_mapped", "permit_type_desc", "description", "valuation", "applied_at",
              "issued_at", "expires_at", "completed_at", "address", "city", "zip_code", "detail_url",
              "processing_state")
DETAIL_FIELDS = ("state", "latitude", "longitude", "housing_units", "housing_units_added",
                 "housing_units_removed", "housing_category", "work_class", "contractor_name",
                 "warnings", "state_updated_at", "state_error", "source_updated_at", "synced_at")


def iso(value: Optional[datetime]) -> Optional[str]:
    """Naive-UTC datetime -> '2026-01-15T00:00:00Z'."""
    return None if value is None else value.isoformat() + "Z"


def _jsonable(value: Any) -> Any:
    return iso(value) if isinstance(value, datetime) else value


def permit_row(p) -> Dict[str, Any]:
    return {f: _jsonable(getattr(p, f)) for f in ROW_FIELDS}


def permit_detail(p) -> Dict[str, Any]:
    out = permit_row(p)
    out.update({f: _jsonable(getattr(p, f)) for f in DETAIL_FIELDS})
    out["warnings"] = out["warnings"] or []
    return out


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, "trace_id": current_trace_id.get(), **fields}, default=str))


class DbNluCache:
    """NluCache backed by the `nlu_cache` table (one short transaction per call)."""

    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, key: str) -> Optional[dict]:
        with self._sessions() as s:
            return NluCacheRepository(s).get(key)

    def put(self, key, question, jurisdiction, schema_version, model, response, ttl) -> None:
        with self._sessions() as s:
            NluCacheRepository(s).put(key, question, jurisdiction, schema_version, model, response, ttl)
            s.commit()


class RepoBackend:
    """RetrievalBackend over PermitRepository (rows serialized with permit_row)."""

    def __init__(self, session):
        self.repo = PermitRepository(session)

    def structured(self, q: PermitQuery, limit: int):
        return [permit_row(p) for p in self.repo.list(replace(q, page_size=min(limit, 200),
                                                               page=None, cursor=None)).rows]

    def text_ranked(self, q, text, limit):
        return [(permit_row(p), s) for p, s in self.repo.text_ranked(q, text, limit)]

    def vector_ranked(self, q, embedding, limit):
        return [(permit_row(p), s) for p, s in self.repo.vector_ranked(q, embedding, limit)]

    def has_embeddings(self, jurisdiction: str) -> bool:
        return self.repo.has_embeddings(jurisdiction)


def _as_list(value) -> List[str]:
    return [value] if isinstance(value, str) else list(value or [])


def trade_coverage(candidates: int, inferred: int) -> Dict[str, Any]:
    """The `trade_coverage` block: the trade filter only sees permits with a skill inference."""
    missing = candidates - inferred
    note = None
    if missing:
        note = (f"{missing:,} of {candidates:,} permits matching the other filters have no skill "
                "inference, so the trade filter cannot match them; run `pts skills infer` / "
                "`pts skills batch` on them to include them.")
    return {"candidates": candidates, "inferred": inferred, "not_inferred": missing, "note": note}


def build_query(jurisdiction: str, params: Mapping[str, Any]) -> PermitQuery:
    """Strictly validated PermitQuery (QueryError on any bad parameter). No DB access."""
    return parse_params(jurisdiction, params, strict=True)[0]


def build_status_counts_query(jurisdiction: str, since=None, until=None, date_field: str = "applied_at",
                              permit_class=None, permit_type=None) -> PermitQuery:
    """The PermitQuery whose rows status_counts groups: window on applied_at or issued_at. No DB access."""
    if date_field not in DATE_FIELDS:
        raise QueryError(E.INVALID_PARAMETER, f"date_field must be one of {', '.join(DATE_FIELDS)}")
    try:
        since_dt = parse_date(since) if since else None
        until_dt = parse_date(until) if until else None
    except ValueError as e:
        raise QueryError(E.INVALID_PARAMETER, f"since/until: {e}") from e
    check_window(since_dt, until_dt)
    prefix = date_field.removesuffix("_at")
    params = {f"{prefix}_after": since_dt, f"{prefix}_before": until_dt,
              "permit_class": permit_class, "permit_type": permit_type}
    return build_query(jurisdiction, params)


def default_embedder(text: str) -> List[float]:
    from permit_trade_skills.embeddings import compute_embedding

    return compute_embedding(text)


class QueryService:
    def __init__(self, db_url: Optional[str] = None, *, nlu: Optional[NLUParser] = None,
                 embedder=default_embedder, settings: Optional[Dict[str, Any]] = None):
        self._sessions = session_factory(db_url)
        self._nlu = nlu
        self._settings = settings
        self.embedder = embedder
        self._known: Optional[tuple] = None   # (expires_monotonic, values)

    # ---- plumbing -----------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Any]:
        """Read session; close() ends the transaction and detaches loaded rows (still readable)."""
        s = self._sessions()
        try:
            yield s
        finally:
            s.close()

    @property
    def nlu(self) -> NLUParser:
        """The NLU parser; built lazily from config/settings.yaml (`nlu.parser` role)."""
        if self._nlu is None:
            from permit_trade_skills.llm import get_provider
            from permit_trade_skills.settings import load_settings

            settings = self._settings or load_settings()
            self._nlu = NLUParser(get_provider("nlu.parser", settings), DbNluCache(self._sessions),
                                  ttl=timedelta(hours=settings["nlu"]["cache_ttl_hours"]))
        return self._nlu

    def _require_jurisdiction(self, session, slug: Optional[str]):
        if not slug:
            raise QueryError(E.INVALID_PARAMETER, "jurisdiction is required")
        j = JurisdictionRepository(session).get(slug)
        if j is None or not j.is_active:
            registered = [x.id for x in JurisdictionRepository(session).list_active()]
            raise QueryError(E.JURISDICTION_NOT_FOUND,
                             f"Jurisdiction {slug!r} is not registered or not active.", 404,
                             extra={"registered_jurisdictions": registered})
        return j

    def resolve_default_jurisdiction(self, slug: Optional[str]) -> str:
        """`slug` if given (validated), else the only active jurisdiction, else JURISDICTION_REQUIRED."""
        with self.session() as s:
            if slug:
                return self._require_jurisdiction(s, slug).id
            active = [j.id for j in JurisdictionRepository(s).list_active()]
        if len(active) == 1:
            return active[0]
        raise QueryError(E.JURISDICTION_REQUIRED,
                         "Specify a jurisdiction: " + (", ".join(active) or "none are registered"),
                         extra={"registered_jurisdictions": active})

    # ---- jurisdictions ------------------------------------------------------

    def list_jurisdictions(self) -> Dict[str, Any]:
        with self.session() as s:
            repo = JurisdictionRepository(s)
            active = repo.list_active()
            stats = repo.stats([j.id for j in active])
        rows = [{
            "slug": j.id, "name": j.name, "county": j.county, "state": j.state,
            "platform": j.platform_type, "dataset_id": j.dataset_id,
            "record_count": stats[j.id][0], "last_synced_at": iso(stats[j.id][1]),
        } for j in active]
        return {"jurisdictions": rows, "count": len(rows)}

    # ---- structured search --------------------------------------------------

    def search(self, jurisdiction: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        return self.execute(build_query(jurisdiction, params))

    def execute(self, q: PermitQuery) -> Dict[str, Any]:
        """Run a PermitQuery: {jurisdiction, query, total_estimate, count, next_cursor, rows}."""
        started = time.monotonic()
        with self.session() as s:
            self._require_jurisdiction(s, q.jurisdiction)
            try:
                page = PermitRepository(s).list(q)
            except ValueError as e:
                raise QueryError(E.INVALID_CURSOR if "cursor" in str(e) else E.INVALID_PARAMETER, str(e)) from e
            rows = [permit_row(p) for p in page.rows]
            counts = None
            if q.trade:
                repo = PermitRepository(s)
                counts = repo.trade_coverage(q)
                found = repo.trade_confidences([p.id for p in page.rows], _as_list(q.trade))
                for p, row in zip(page.rows, rows):
                    row["trade_confidence"] = found.get(p.id, {})
        # The planner estimate can undershoot; never report fewer rows than this page proves exist.
        # With a trade filter the exact match count is known (the planner can't estimate EXISTS).
        seen = len(rows) + (1 if page.next_cursor else 0)
        total = counts[2] if counts else max(page.total_estimate, seen)
        coverage = trade_coverage(counts[0], counts[1]) if counts else None
        result = {"jurisdiction": q.jurisdiction, "query": q.to_dict(),
                  "total_estimate": total, "count": len(rows),
                  "next_cursor": page.next_cursor, "rows": rows, "trade_coverage": coverage}
        _log("query.search", query=result["query"], count=len(rows),
             total_estimate=result["total_estimate"], permit_numbers=[r["permit_number"] for r in rows],
             duration_ms=int((time.monotonic() - started) * 1000))
        return result

    # ---- single permit ------------------------------------------------------

    def get_permit(self, jurisdiction: str, permit_number: str) -> Dict[str, Any]:
        with self.session() as s:
            self._require_jurisdiction(s, jurisdiction)
            repo = PermitRepository(s)
            p = repo.get(jurisdiction, permit_number.strip())
            if p is None:
                raise QueryError(E.PERMIT_NOT_FOUND,
                                 f"Permit {permit_number!r} not found in {jurisdiction}.", 404)
            history = [{
                "from_status": h.from_status, "to_status": h.to_status, "raw_status": h.raw_status,
                "changed_at": iso(h.changed_at), "changed_by": h.changed_by, "run_id": h.run_id,
            } for h in repo.status_history(p.id)]
            snap = SnapshotRepository(s).latest_for(p.id)
            inf = InferenceRepository(s).latest_for(p.id)
            result = {
                "jurisdiction": jurisdiction,
                "permit": permit_detail(p),
                "status_history": history,
                "detail_snapshot": ({"present": True, "fetched_at": iso(snap.fetched_at),
                                     "final_url": snap.final_url, "fetch_strategy": snap.fetch_strategy}
                                    if snap else {"present": False}),
                "skill_inference": ({"id": inf.id, "model": inf.model, "prompt_version": inf.prompt_version,
                                     "review_status": inf.review_status, "created_at": iso(inf.created_at),
                                     "skills": inf.skills or [], "assumptions": inf.assumptions or []}
                                    if inf else None),
            }
        _log("query.get_permit", jurisdiction=jurisdiction, permit_number=permit_number)
        return result

    # ---- status counts ------------------------------------------------------

    def status_counts(self, jurisdiction: str, since=None, until=None, *,
                      date_field: str = "applied_at", permit_class=None, permit_type=None) -> Dict[str, Any]:
        q = build_status_counts_query(jurisdiction, since, until, date_field, permit_class, permit_type)
        with self.session() as s:
            self._require_jurisdiction(s, jurisdiction)
            raw = PermitRepository(s).status_counts(q)
        counts = {status: raw.get(status, 0) for status in DISPLAY_STATUSES}
        if raw.get("UNKNOWN"):
            counts["UNKNOWN"] = raw["UNKNOWN"]
        prefix = date_field.removesuffix("_at")
        after, before = getattr(q, f"{prefix}_after"), getattr(q, f"{prefix}_before")
        result = {
            "jurisdiction": jurisdiction, "since": iso(after), "until": iso(before),
            "date_field": date_field,
            "filters": {k: v for k, v in (("permit_class", q.permit_class), ("permit_type", q.permit_type)) if v},
            "counts": counts, "total": sum(counts.values()),
        }
        _log("query.status_counts", query=q.to_dict(), counts=counts)
        return result

    # ---- hybrid retrieval ---------------------------------------------------

    def retrieve(self, jurisdiction: str, params: Mapping[str, Any], text: Optional[str],
                 k: int = 10, modes: Sequence[str] = DEFAULT_MODES) -> RetrievalResult:
        q = build_query(jurisdiction, params)
        with self.session() as s:
            self._require_jurisdiction(s, jurisdiction)
            result = retrieve(q, text, k, modes, backend=RepoBackend(s), embedder=self.embedder)
        _log("query.retrieve", retrieval_id=result.retrieval_id, query=result.query, text=text,
             modes_used=result.modes_used, degraded=result.degraded,
             candidate_counts=result.candidate_counts, fused_order=result.fused_order)
        return result

    # ---- natural language ---------------------------------------------------

    def _active_jurisdictions(self) -> List[Dict[str, Any]]:
        with self.session() as s:
            return [{"slug": j.id, "name": j.name, "county": j.county, "state": j.state}
                    for j in JurisdictionRepository(s).list_active()]

    def known_values(self, slugs: Sequence[str]) -> Dict[str, List[str]]:
        """Distinct class/type/trade values shown to the NLU (cached in-process for 10 minutes)."""
        now = time.monotonic()
        if self._known and self._known[0] > now and self._known[1][0] == tuple(slugs):
            return self._known[1][1]
        with self.session() as s:
            repo = PermitRepository(s)
            classes = sorted(set(repo.distinct_values(slugs, "permit_class_mapped"))
                             | set(repo.distinct_values(slugs, "permit_class")))
            trades = {t.lower(): t for t in repo.distinct_trades(slugs)}
            trades.update({t.lower(): t for t in TRADES})   # canonical spelling wins
            values = {"permit_class": classes,
                      "permit_type": repo.distinct_values(slugs, "permit_type_mapped"),
                      "trade": sorted(trades.values())}
        self._known = (now + KNOWN_VALUES_TTL_S, (tuple(slugs), values))
        return values

    def ask(self, question: str, jurisdiction: Optional[str] = None, *,
            page_size: Optional[int] = None, cursor: Optional[str] = None) -> Dict[str, Any]:
        question = (question or "").strip()
        if not question:
            raise QueryError(E.INVALID_PARAMETER, "question is empty")
        active = self._active_jurisdictions()
        slugs = [j["slug"] for j in active]
        if jurisdiction and jurisdiction not in slugs:
            raise QueryError(E.JURISDICTION_NOT_FOUND,
                             f"Jurisdiction {jurisdiction!r} is not registered or not active.", 404,
                             extra={"registered_jurisdictions": slugs})

        parsed = self.nlu.parse(question, jurisdiction, self.known_values(slugs))
        assumptions = list(parsed.assumptions)

        # Jurisdiction: never query a different place than the one named.
        named = parsed.jurisdiction_in_question
        matches = match_jurisdictions(named, active) if named else []
        if named and not matches:
            raise QueryError(E.JURISDICTION_NOT_REGISTERED,
                             f"The question names {named!r}, which is not a registered jurisdiction. "
                             f"Registered: {', '.join(slugs) or 'none'}.",
                             extra={"registered_jurisdictions": slugs})
        if named and jurisdiction and jurisdiction not in matches:
            raise QueryError(E.JURISDICTION_CONFLICT,
                             f"The question names {named!r} but the request's jurisdiction is "
                             f"{jurisdiction!r}.", extra={"candidates": matches})
        if jurisdiction:
            slug = jurisdiction
        elif len(matches) == 1:
            slug = matches[0]
        elif len(matches) > 1:
            raise QueryError(E.JURISDICTION_REQUIRED,
                             f"{named!r} matches several jurisdictions; pass `jurisdiction`.",
                             extra={"candidates": matches})
        elif len(slugs) == 1:
            slug = slugs[0]
            assumptions.append(f"no jurisdiction named; used {slug} (the only registered jurisdiction)")
        else:
            raise QueryError(E.JURISDICTION_REQUIRED,
                             "The question does not name a jurisdiction; pass `jurisdiction` "
                             f"(registered: {', '.join(slugs) or 'none'}).",
                             extra={"registered_jurisdictions": slugs})

        extra = {k: v for k, v in (("page_size", page_size), ("cursor", cursor)) if v is not None}
        q, problems, more = interpret(parsed, next(j for j in active if j["slug"] == slug), extra)
        assumptions += more
        unfulfilled = list(parsed.unrecognized_entities) + problems

        nlu_info = {"model": parsed.model, "prompt_version": PROMPT_VERSION,
                    "schema_version": SCHEMA_VERSION, "cached": parsed.cached,
                    "request_id": parsed.request_id}
        common = {"question": question, "interpreted": parsed.permit_query,
                  "unfulfilled_filters": unfulfilled, "assumptions": assumptions,
                  "ambiguous_terms": parsed.ambiguous_terms, "nlu": nlu_info}

        if parsed.core_requirement_unfulfillable and unfulfilled:
            result = {"jurisdiction": slug, "query": q.to_dict(), "total_estimate": 0, "count": 0,
                      "next_cursor": None, "rows": [], "executed": False,
                      "echo": "No query was run: the question's main requirement cannot be expressed "
                              "with the available filters (see unfulfilled_filters).",
                      **common}
        else:
            result = {**self.execute(q), "executed": True,
                      "echo": render_echo(q, suffix="(interpreted from your question)"), **common}
        clarify = result["count"] == 0 and unfulfilled
        if clarify:
            text = parsed.clarification or (
                "Some of your question could not be expressed as a permit filter: "
                + "; ".join(unfulfilled) + ".")
            result["clarification"] = {"text": text, "llm_generated": bool(parsed.clarification)}
        else:
            result["clarification"] = None
        _log("query.ask", question=question, jurisdiction=slug, query=result["query"],
             executed=result["executed"], count=result["count"], unfulfilled_filters=unfulfilled,
             nlu=nlu_info)
        return result

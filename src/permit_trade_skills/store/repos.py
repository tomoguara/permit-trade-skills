"""Repositories: the data-access API for the canonical store.

Repositories work inside a caller-owned Session; the caller decides the
transaction boundary (e.g. ``with store.transaction() as s: ...`` for one batch,
or one transaction for a whole sync run). Write methods return a rowcount (or
the written id) so callers can verify effects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from permit_trade_skills.discovery.registry import (
    DetailSnapshotTable,
    JurisdictionTable,
    NluCacheTable,
    PermitTable,
    SkillInferenceTable,
    StatusHistoryTable,
    SyncMetadataTable,
)
from permit_trade_skills.ingest.schemas import ProcessingState
from permit_trade_skills.store.query import (
    CONFIDENCE_LEVELS,
    SORT_COLUMNS,
    CursorPage,
    PermitQuery,
    decode_cursor,
    encode_cursor,
)

_STATES = {s.value for s in ProcessingState}


def _as_list(value) -> Optional[List[str]]:
    if value is None:
        return None
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _utcnow() -> datetime:
    """Naive UTC, matching the schema's naive-UTC DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _check_state(state: str) -> str:
    if state not in _STATES:
        raise ValueError(f"unknown processing state {state!r}; expected one of {sorted(_STATES)}")
    return state


def _latest_inference_id():
    """Correlated scalar subquery: the id of the outer permit's newest skill inference."""
    SI = SkillInferenceTable
    return (select(SI.id).where(SI.permit_id == PermitTable.id)
            .order_by(SI.created_at.desc(), SI.id.desc()).limit(1)
            .correlate(PermitTable).scalar_subquery())


def _usable_inference(latest_id, *extra):
    """EXISTS: the latest inference (by id) is present, not REJECTED, and meets `extra`."""
    SI = SkillInferenceTable
    return select(SI.id).where(SI.id == latest_id, SI.review_status != "REJECTED", *extra).exists()


def _trade_condition(trades: Sequence[str], min_confidence: Optional[str]):
    """The permit's latest usable inference lists a skill in one of `trades`."""
    SI = SkillInferenceTable
    skill = func.jsonb_array_elements(SI.skills).table_valued("value").alias("skill")
    conds = [func.lower(skill.c.value.op("->>")("trade")).in_([t.strip().lower() for t in trades])]
    if min_confidence:
        allowed = CONFIDENCE_LEVELS[CONFIDENCE_LEVELS.index(min_confidence):]
        conds.append(skill.c.value.op("->>")("confidence").in_(allowed))
    has_skill = select(skill.c.value).select_from(skill).where(*conds).exists()
    return _usable_inference(_latest_inference_id(), has_skill)


def _trade_conditions(q: PermitQuery) -> list:
    """The trade filter: one condition for "any" of q.trade, one per trade for "all"."""
    trades = _as_list(q.trade)
    if not trades:
        return []
    if q.trade_match == "all":
        return [_trade_condition([t], q.trade_min_confidence) for t in trades]
    return [_trade_condition(trades, q.trade_min_confidence)]


class PermitRepository:
    def __init__(self, session: Session):
        self.session = session

    # ---- reads -------------------------------------------------------------

    def get(self, jurisdiction: str, permit_number: str) -> Optional[PermitTable]:
        """One permit by jurisdiction slug + source permit number."""
        return self.session.execute(
            select(PermitTable).where(
                PermitTable.jurisdiction_slug == jurisdiction,
                PermitTable.permit_number == permit_number,
            )
        ).scalar_one_or_none()

    def _filters(self, q: PermitQuery) -> list:
        P = PermitTable
        conds = [P.jurisdiction_slug == q.jurisdiction]
        if (statuses := _as_list(q.status)):
            conds.append(P.status.in_([s.upper() for s in statuses]))
        if (excluded := _as_list(q.statuses_exclude)):
            conds.append(P.status.notin_([s.upper() for s in excluded]))
        if (classes := _as_list(q.permit_class)):
            lowered = [c.lower() for c in classes]
            conds.append(or_(func.lower(P.permit_class).in_(lowered),
                             func.lower(P.permit_class_mapped).in_(lowered)))
        if (types := _as_list(q.permit_type)):
            lowered = [t.lower() for t in types]
            conds.append(or_(func.lower(P.permit_type_mapped).in_(lowered),
                             func.lower(P.permit_type_desc).in_(lowered)))
        if q.desc_keyword:
            conds.append(P.description.ilike(f"%{q.desc_keyword}%"))
        conds.extend(_trade_conditions(q))
        if q.applied_after is not None:
            conds.append(P.applied_at >= q.applied_after)
        if q.applied_before is not None:
            conds.append(P.applied_at < q.applied_before)
        if q.issued_after is not None:
            conds.append(P.issued_at >= q.issued_after)
        if q.issued_before is not None:
            conds.append(P.issued_at < q.issued_before)
        if q.zip:
            conds.append(P.zip_code == q.zip)
        if q.city:
            conds.append(P.city.ilike(f"%{q.city}%"))
        if q.min_cost is not None:
            conds.append(P.valuation >= q.min_cost)
        if q.max_cost is not None:
            conds.append(P.valuation <= q.max_cost)
        if (states := _as_list(q.state)):
            conds.append(P.processing_state.in_([_check_state(s) for s in states]))
        return conds

    def _order(self, q: PermitQuery):
        col_name, desc = SORT_COLUMNS[q.sort]
        col = getattr(PermitTable, col_name)
        if desc:
            return col, [col.desc().nulls_last(), PermitTable.id.desc()], desc
        return col, [col.asc().nulls_last(), PermitTable.id.asc()], desc

    @staticmethod
    def _after(col, desc: bool, value, is_null: bool, row_id: int):
        """Keyset predicate: rows strictly after (value, id) in the sort order."""
        P = PermitTable
        if desc:
            if is_null:  # already in the NULL tail (sorted by id DESC)
                return and_(col.is_(None), P.id < row_id)
            return or_(col < value, and_(col == value, P.id < row_id), col.is_(None))
        if is_null:
            return and_(col.is_(None), P.id > row_id)
        return or_(tuple_(col, P.id) > tuple_(value, row_id), col.is_(None))

    def list(self, q: PermitQuery) -> CursorPage[PermitTable]:
        """Filtered, sorted, keyset-paged permits (no OFFSET)."""
        col, order_by, desc = self._order(q)
        conds = self._filters(q)
        col_name = SORT_COLUMNS[q.sort][0]

        cursor = None
        if q.cursor:
            cursor = decode_cursor(q.cursor)
            if cursor.sort != q.sort.value:
                raise ValueError("cursor was issued for a different sort")
        elif q.page and q.page > 1:
            # Locate the start of page N by reading only the sort keys of the
            # preceding rows (index-only where possible), then continue by keyset.
            skip = (q.page - 1) * q.page_size
            keys = self.session.execute(
                select(col, PermitTable.id).where(*conds).order_by(*order_by).limit(skip)
            ).all()
            if len(keys) < skip:
                return CursorPage(rows=[], next_cursor=None, total_estimate=self.estimate(q))
            last_value, last_id = keys[-1]
            cursor = decode_cursor(encode_cursor(q.sort, last_value, last_id))

        if cursor is not None:
            conds.append(self._after(col, desc, cursor.value, cursor.is_null, cursor.id))

        rows = list(self.session.execute(
            select(PermitTable).where(*conds).order_by(*order_by).limit(q.page_size + 1)
        ).scalars())
        next_cursor = None
        if len(rows) > q.page_size:
            rows = rows[:q.page_size]
            last = rows[-1]
            next_cursor = encode_cursor(q.sort, getattr(last, col_name), last.id)
        return CursorPage(rows=rows, next_cursor=next_cursor, total_estimate=self.estimate(q))

    def estimate(self, q: PermitQuery) -> int:
        """Planner row estimate for the query's filters (cheap; not an exact count)."""
        stmt = select(PermitTable.id).where(*self._filters(q))
        conn = self.session.connection()
        # render_postcompile expands IN-lists into individual bound parameters
        compiled = stmt.compile(dialect=conn.dialect, compile_kwargs={"render_postcompile": True})
        plan = conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + str(compiled), compiled.params).scalar()
        if isinstance(plan, str):
            plan = json.loads(plan)
        return int(plan[0]["Plan"]["Plan Rows"])

    def trade_coverage(self, q: PermitQuery) -> Tuple[int, int, int]:
        """Exact (candidates, inferred, matched) counts for a query with a trade filter.

        candidates: permits matching every filter except trade; inferred: those
        whose latest skill inference exists and is not REJECTED (all the trade
        filter can see); matched: those the full query matches.
        """
        base = replace(q, trade=None, trade_min_confidence=None)
        inferred = _usable_inference(_latest_inference_id())
        matched = and_(*_trade_conditions(q))
        total, seen, hits = self.session.execute(
            select(func.count(), func.count().filter(inferred), func.count().filter(matched))
            .select_from(PermitTable).where(*self._filters(base))
        ).one()
        return int(total), int(seen), int(hits)

    def status_counts(self, q: PermitQuery) -> Dict[str, int]:
        """Exact `count(*) ... GROUP BY status` over the query's filters (sort/paging ignored)."""
        rows = self.session.execute(
            select(PermitTable.status, func.count()).where(*self._filters(q)).group_by(PermitTable.status)
        ).all()
        return {status: int(n) for status, n in rows}

    def text_ranked(self, q: PermitQuery, text: str, limit: int) -> List[Tuple[PermitTable, float]]:
        """Permits whose description contains `text` (ILIKE, trigram GIN), within the
        query's filters, ranked by trigram word_similarity desc, then id."""
        P = PermitTable
        similarity = func.word_similarity(text, P.description)
        rows = self.session.execute(
            select(P, similarity.label("similarity"))
            .where(*self._filters(q), P.description.ilike(f"%{text}%"))
            .order_by(similarity.desc(), P.id)
            .limit(limit)
        ).all()
        return [(permit, float(sim)) for permit, sim in rows]

    def vector_ranked(self, q: PermitQuery, embedding: Sequence[float],
                      limit: int) -> List[Tuple[PermitTable, float]]:
        """Embedded permits within the query's filters, ranked by cosine similarity
        (1 - cosine distance) to `embedding`, then id. Exact scan (no ANN index)."""
        P = PermitTable
        distance = P.description_embedding.cosine_distance(list(embedding))
        rows = self.session.execute(
            select(P, distance.label("distance"))
            .where(*self._filters(q), P.description_embedding.isnot(None))
            .order_by(distance, P.id)
            .limit(limit)
        ).all()
        return [(permit, 1.0 - float(dist)) for permit, dist in rows]

    def distinct_values(self, jurisdictions: Sequence[str], column: str, limit: int = 50) -> List[str]:
        """Distinct non-null values of a permits text column (e.g. permit_type_mapped), sorted."""
        if column not in ("permit_class", "permit_class_mapped", "permit_type_mapped", "permit_type_desc"):
            raise ValueError(f"distinct_values not supported for {column!r}")
        col = getattr(PermitTable, column)
        return list(self.session.execute(
            select(col).where(PermitTable.jurisdiction_slug.in_(list(jurisdictions)), col.isnot(None))
            .group_by(col).order_by(col).limit(limit)
        ).scalars())

    def trade_confidences(self, permit_ids: Sequence[int],
                          trades: Sequence[str]) -> Dict[int, Dict[str, str]]:
        """{permit_id: {trade: best confidence}} for `trades` (spelled as given), from each
        permit's latest non-REJECTED inference; trades it does not list are omitted."""
        if not permit_ids or not trades:
            return {}
        SI = SkillInferenceTable
        latest = (select(SI.permit_id, SI.skills, SI.review_status)
                  .where(SI.permit_id.in_(list(permit_ids)))
                  .order_by(SI.permit_id, SI.created_at.desc(), SI.id.desc())
                  .distinct(SI.permit_id))
        wanted = {t.strip().lower(): t.strip() for t in trades}
        out: Dict[int, Dict[str, str]] = {}
        for permit_id, skills, review_status in self.session.execute(latest):
            if review_status == "REJECTED":
                continue
            best: Dict[str, str] = {}
            for skill in skills or []:
                name = wanted.get(str(skill.get("trade") or "").strip().lower())
                level = skill.get("confidence")
                if name and level in CONFIDENCE_LEVELS and (
                        name not in best or CONFIDENCE_LEVELS.index(level) > CONFIDENCE_LEVELS.index(best[name])):
                    best[name] = level
            out[permit_id] = {t: best[t] for t in wanted.values() if t in best}
        return out

    def distinct_trades(self, jurisdictions: Sequence[str], limit: int = 100) -> List[str]:
        """Distinct `trade` values in the jurisdictions' skill inferences, sorted."""
        SI, P = SkillInferenceTable, PermitTable
        skill = func.jsonb_array_elements(SI.skills).table_valued("value").alias("skill")
        trade = skill.c.value.op("->>")("trade")
        return list(self.session.execute(
            select(trade).select_from(SI).join(P, P.id == SI.permit_id).join(skill, true())
            .where(P.jurisdiction_slug.in_(list(jurisdictions)), trade.isnot(None))
            .group_by(trade).order_by(trade).limit(limit)
        ).scalars())

    def has_embeddings(self, jurisdiction: str) -> bool:
        """True if any permit in the jurisdiction has a description embedding."""
        P = PermitTable
        return bool(self.session.execute(
            select(P.id).where(P.jurisdiction_slug == jurisdiction,
                               P.description_embedding.isnot(None)).limit(1)
        ).first())

    def status_history(self, permit_id: int) -> List[StatusHistoryTable]:
        """A permit's status changes, newest first."""
        H = StatusHistoryTable
        return list(self.session.execute(
            select(H).where(H.permit_id == permit_id).order_by(H.changed_at.desc(), H.id.desc())
        ).scalars())

    # ---- lifecycle writes --------------------------------------------------

    def advance_state(self, permit_id: int, from_state: str, to_state: str,
                      error: Optional[dict] = None) -> bool:
        """Optimistic compare-and-set of processing_state.

        UPDATE ... WHERE id = :id AND processing_state = :from. Returns False
        (no change) if the permit was not in `from_state`, e.g. because a
        concurrent worker advanced it first. `error` is stored in state_error
        (cleared when None).
        """
        _check_state(from_state)
        _check_state(to_state)
        result = self.session.execute(
            update(PermitTable)
            .where(PermitTable.id == permit_id, PermitTable.processing_state == from_state)
            .values(processing_state=to_state, state_updated_at=_utcnow(), state_error=error)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def touch(self, permit_ids: Iterable[int], state: str) -> int:
        """Batch-set processing_state for many permits. Returns rows updated."""
        _check_state(state)
        ids = list(permit_ids)
        if not ids:
            return 0
        result = self.session.execute(
            update(PermitTable)
            .where(PermitTable.id.in_(ids))
            .values(processing_state=state, state_updated_at=_utcnow(), state_error=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount


class SnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def latest_for(self, permit_id: int) -> Optional[DetailSnapshotTable]:
        return self.session.execute(
            select(DetailSnapshotTable)
            .where(DetailSnapshotTable.permit_id == permit_id)
            .order_by(DetailSnapshotTable.fetched_at.desc(), DetailSnapshotTable.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def upsert_snapshot(self, permit_id: int, final_url: str, fetch_strategy: str,
                        content_text: str, raw_html: Optional[str] = None,
                        http_status: Optional[int] = None, error: Optional[str] = None,
                        content_hash: Optional[str] = None) -> int:
        """Insert a snapshot, or refresh the existing one with the same content.

        Keyed on (permit_id, content_hash); content_hash defaults to
        sha256(content_text). Returns the snapshot id.
        """
        content_hash = content_hash or hashlib.sha256(content_text.encode()).hexdigest()
        T = DetailSnapshotTable.__table__
        stmt = pg_insert(T).values(
            permit_id=permit_id, final_url=final_url, fetch_strategy=fetch_strategy,
            content_text=content_text, raw_html=raw_html, content_hash=content_hash,
            fetched_at=_utcnow(), http_status=http_status, error=error,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_detail_snapshots_permit_hash",
            set_={
                "final_url": stmt.excluded.final_url,
                "fetched_at": stmt.excluded.fetched_at,
                "http_status": stmt.excluded.http_status,
                "error": stmt.excluded.error,
                "raw_html": func.coalesce(stmt.excluded.raw_html, T.c.raw_html),
            },
        ).returning(T.c.id)
        return self.session.execute(stmt).scalar_one()


class InferenceRepository:
    def __init__(self, session: Session):
        self.session = session

    def latest_for(self, permit_id: int) -> Optional[SkillInferenceTable]:
        return self.session.execute(
            select(SkillInferenceTable)
            .where(SkillInferenceTable.permit_id == permit_id)
            .order_by(SkillInferenceTable.created_at.desc(), SkillInferenceTable.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def upsert_inference(self, permit_id: int, prompt_version: str, model: str,
                         skills: Sequence[dict], assumptions: Sequence[Any] = (),
                         request_id: Optional[str] = None,
                         input_snapshot_id: Optional[int] = None) -> int:
        """Record an inference. Idempotent per (permit_id, request_id) when request_id is given.

        Returns the inference id (the existing one on a repeated request_id).
        """
        if request_id is not None:
            existing = self.session.execute(
                select(SkillInferenceTable.id).where(
                    SkillInferenceTable.permit_id == permit_id,
                    SkillInferenceTable.request_id == request_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        row = SkillInferenceTable(
            permit_id=permit_id, prompt_version=prompt_version, model=model,
            request_id=request_id, skills=list(skills), assumptions=list(assumptions),
            input_snapshot_id=input_snapshot_id,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def pending_inferences(self, limit: int = 100,
                           jurisdiction: Optional[str] = None) -> List[SkillInferenceTable]:
        """Oldest-first inferences awaiting review (served by the partial PENDING index)."""
        stmt = select(SkillInferenceTable).where(SkillInferenceTable.review_status == "PENDING")
        if jurisdiction:
            stmt = stmt.join(PermitTable, PermitTable.id == SkillInferenceTable.permit_id).where(
                PermitTable.jurisdiction_slug == jurisdiction
            )
        stmt = stmt.order_by(SkillInferenceTable.created_at, SkillInferenceTable.id).limit(limit)
        return list(self.session.execute(stmt).scalars())


class JurisdictionRepository:
    """Read access to the jurisdiction registry (Stage 1 owns the writes)."""

    def __init__(self, session: Session):
        self.session = session

    def get(self, slug: str) -> Optional[JurisdictionTable]:
        return self.session.get(JurisdictionTable, slug)

    def list_active(self) -> List[JurisdictionTable]:
        return list(self.session.execute(
            select(JurisdictionTable).where(JurisdictionTable.is_active.is_(True))
            .order_by(JurisdictionTable.id)
        ).scalars())

    def stats(self, slugs: Sequence[str]) -> Dict[str, Tuple[int, Optional[datetime]]]:
        """slug -> (exact permit count, completed_at of the latest successful sync run)."""
        if not slugs:
            return {}
        counts = dict(self.session.execute(
            select(PermitTable.jurisdiction_slug, func.count())
            .where(PermitTable.jurisdiction_slug.in_(list(slugs)))
            .group_by(PermitTable.jurisdiction_slug)
        ).all())
        synced = dict(self.session.execute(
            select(SyncMetadataTable.jurisdiction_slug, func.max(SyncMetadataTable.completed_at))
            .where(SyncMetadataTable.jurisdiction_slug.in_(list(slugs)),
                   SyncMetadataTable.error.is_(None))
            .group_by(SyncMetadataTable.jurisdiction_slug)
        ).all())
        return {s: (int(counts.get(s, 0)), synced.get(s)) for s in slugs}


class NluCacheRepository:
    """Key/value cache for natural-language parses with a TTL (Stage 04)."""

    def __init__(self, session: Session):
        self.session = session

    def get(self, key: str) -> Optional[dict]:
        """The cached response, or None if absent or expired."""
        T = NluCacheTable
        return self.session.execute(
            select(T.response).where(T.key == key, T.expires_at > _utcnow())
        ).scalar_one_or_none()

    def put(self, key: str, question: str, jurisdiction: Optional[str], schema_version: str,
            model: str, response: dict, ttl: timedelta) -> None:
        """Insert or replace the entry for `key`, expiring `ttl` from now."""
        T = NluCacheTable.__table__
        now = _utcnow()
        values = dict(key=key, question=question, jurisdiction=jurisdiction,
                      schema_version=schema_version, model=model, response=response,
                      created_at=now, expires_at=now + ttl)
        stmt = pg_insert(T).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[T.c.key],
            set_={k: stmt.excluded[k] for k in values if k != "key"},
        )
        self.session.execute(stmt)

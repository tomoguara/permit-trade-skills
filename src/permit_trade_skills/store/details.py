"""SQL implementation of the detail stage's persistence port (`detail.ports.DetailStore`).

Each method runs in its own short transaction, so a batch's per-permit writes
are independent (one permit's failure never rolls back another's snapshot).
Returns plain dicts; does not import `permit_trade_skills.detail`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from permit_trade_skills.discovery.registry import (
    DetailFetchResultTable,
    DetailFetchRunTable,
    DetailSnapshotTable,
    JurisdictionTable,
    PermitTable,
)
from permit_trade_skills.store import transaction

PERMIT_COLUMNS = ("id", "jurisdiction_id", "permit_number", "detail_url", "raw_row", "raw_hash", "status",
                  "processing_state", "address", "description", "issued_at", "applied_at",
                  "completed_at", "expires_at", "valuation")
SUCCESS_FROM = ("SYNCED", "DETAIL_FAILED", "DETAIL_READY")
FAILURE_FROM = ("SYNCED", "DETAIL_FAILED")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(obj, columns: Sequence[str]) -> Dict[str, Any]:
    return {c: getattr(obj, c) for c in columns}


SNAPSHOT_COLUMNS = tuple(c.name for c in DetailSnapshotTable.__table__.columns)
RUN_COLUMNS = tuple(c.name for c in DetailFetchRunTable.__table__.columns)
RESULT_COLUMNS = tuple(c.name for c in DetailFetchResultTable.__table__.columns)


class SqlDetailStore:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url

    # ---- reads -------------------------------------------------------------

    def jurisdiction(self, slug: str) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            j = s.get(JurisdictionTable, slug)
            if j is None:
                return None
            return _row(j, ("id", "name", "platform_type", "base_url", "api_endpoint", "dataset_id",
                            "field_map", "detail_strategy", "is_active"))

    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            p = s.execute(select(PermitTable).where(PermitTable.jurisdiction_id == jurisdiction,
                                                    PermitTable.permit_number == permit_number)
                          ).scalar_one_or_none()
            return _row(p, PERMIT_COLUMNS) if p is not None else None

    def select_permits(self, jurisdiction: str, *, status: Optional[str] = None,
                       since: Optional[datetime] = None, until: Optional[datetime] = None,
                       limit: int = 25, permit_numbers: Optional[Sequence[str]] = None
                       ) -> List[Dict[str, Any]]:
        P = PermitTable
        stmt = select(P).where(P.jurisdiction_id == jurisdiction)
        if status:
            stmt = stmt.where(P.status == status)
        if since:
            stmt = stmt.where(P.issued_at >= since)
        if until:
            stmt = stmt.where(P.issued_at < until)
        if permit_numbers:
            stmt = stmt.where(P.permit_number.in_(list(permit_numbers)))
        stmt = stmt.order_by(P.issued_at.desc().nulls_last(), P.id.desc()).limit(limit)
        with transaction(self.db_url) as s:
            return [_row(p, PERMIT_COLUMNS) for p in s.execute(stmt).scalars()]

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]:
        T = DetailSnapshotTable
        with transaction(self.db_url) as s:
            snap = s.execute(select(T).where(T.permit_id == permit_id)
                             .order_by(T.fetched_at.desc(), T.id.desc()).limit(1)).scalar_one_or_none()
            return _row(snap, SNAPSHOT_COLUMNS) if snap is not None else None

    # ---- writes ------------------------------------------------------------

    def save_snapshot(self, permit_id: int, document: Dict[str, Any], *, raw_html: Optional[str],
                      raw_responses: Optional[List[Dict[str, Any]]], http_status: Optional[int],
                      source_hash: Optional[str], run_id: Optional[str]) -> Tuple[int, bool, datetime]:
        T = DetailSnapshotTable.__table__
        content_hash = document.get("content_hash") or hashlib.sha256(
            document["content_text"].encode()).hexdigest()
        now = _utcnow()
        values = dict(
            permit_id=permit_id, final_url=document["final_url"], fetch_strategy=document["fetch_strategy"],
            content_text=document["content_text"], content_hash=content_hash, raw_html=raw_html,
            fetched_at=now, http_status=http_status, error=None, fields=document.get("fields") or {},
            degraded=bool(document.get("degraded")), notes=list(document.get("notes") or []),
            signals=document.get("signals"), source_hash=source_hash, raw_responses=raw_responses,
            run_id=run_id,
        )
        stmt = pg_insert(T).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_detail_snapshots_permit_hash",
            set_={
                "final_url": stmt.excluded.final_url,
                "fetch_strategy": stmt.excluded.fetch_strategy,
                "fetched_at": stmt.excluded.fetched_at,
                "http_status": stmt.excluded.http_status,
                "error": None,
                "fields": stmt.excluded.fields,
                "degraded": stmt.excluded.degraded,
                "notes": stmt.excluded.notes,
                "signals": stmt.excluded.signals,
                "source_hash": stmt.excluded.source_hash,
                "run_id": stmt.excluded.run_id,
                "raw_html": func.coalesce(stmt.excluded.raw_html, T.c.raw_html),
                "raw_responses": func.coalesce(stmt.excluded.raw_responses, T.c.raw_responses),
            },
        ).returning(T.c.id, T.c.fetched_at,
                    # xmax = 0 on the returned tuple <=> inserted (not updated by ON CONFLICT)
                    literal_column("(xmax = 0)").label("inserted"))
        with transaction(self.db_url) as s:
            snapshot_id, fetched_at, inserted = s.execute(stmt).one()
            return snapshot_id, bool(inserted), fetched_at

    def mark_detail_state(self, permit_id: int, ok: bool,
                          error: Optional[Dict[str, Any]] = None) -> Optional[str]:
        P = PermitTable
        target, sources = ("DETAIL_READY", SUCCESS_FROM) if ok else ("DETAIL_FAILED", FAILURE_FROM)
        with transaction(self.db_url) as s:
            n = s.execute(
                update(P).where(P.id == permit_id, P.processing_state.in_(sources))
                .values(processing_state=target, state_updated_at=_utcnow(),
                        state_error=None if ok else error)
                .execution_options(synchronize_session=False)
            ).rowcount
        return target if n else None

    # ---- run ledger --------------------------------------------------------

    def create_run(self, run_id: str, jurisdiction: str, trigger: str, params: Dict[str, Any]) -> None:
        R = DetailFetchRunTable.__table__
        with transaction(self.db_url) as s:
            s.execute(pg_insert(R).values(run_id=run_id, jurisdiction_id=jurisdiction, trigger=trigger,
                                          params=params, status="running", started_at=_utcnow())
                      .on_conflict_do_update(index_elements=["run_id"],
                                             set_={"status": "running", "completed_at": None}))

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            run = s.get(DetailFetchRunTable, run_id)
            return _row(run, RUN_COLUMNS) if run is not None else None

    def record_result(self, run_id: str, result: Dict[str, Any]) -> None:
        T = DetailFetchResultTable.__table__
        values = {k: v for k, v in result.items() if k in RESULT_COLUMNS and k not in ("id", "created_at")}
        values["run_id"] = run_id
        stmt = pg_insert(T).values(**values, created_at=_utcnow())
        stmt = stmt.on_conflict_do_update(
            constraint="uq_detail_fetch_results_run_permit",
            set_={k: stmt.excluded[k] for k in values if k not in ("run_id", "permit_id")}
            | {"created_at": stmt.excluded.created_at})
        with transaction(self.db_url) as s:
            s.execute(stmt)

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        T = DetailFetchResultTable
        with transaction(self.db_url) as s:
            counts = dict(s.execute(select(T.outcome, func.count()).where(T.run_id == run_id)
                                    .group_by(T.outcome)).all())
            run = s.get(DetailFetchRunTable, run_id)
            run.ok = counts.get("ok", 0)
            run.failed = counts.get("failed", 0)
            run.skipped = counts.get("skipped", 0)
            run.total = run.ok + run.failed + run.skipped
            run.status = status
            run.error = error
            run.completed_at = _utcnow()
            s.flush()
            return _row(run, RUN_COLUMNS)

    def run_results(self, run_id: str) -> List[Dict[str, Any]]:
        T = DetailFetchResultTable
        with transaction(self.db_url) as s:
            return [_row(r, RESULT_COLUMNS) for r in
                    s.execute(select(T).where(T.run_id == run_id).order_by(T.id)).scalars()]

    def done_permit_ids(self, run_id: str) -> Set[int]:
        T = DetailFetchResultTable
        with transaction(self.db_url) as s:
            return set(s.execute(select(T.permit_id).where(T.run_id == run_id,
                                                           T.outcome.in_(("ok", "skipped")))).scalars())

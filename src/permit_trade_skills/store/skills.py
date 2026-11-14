"""SQL implementation of the skill stage's persistence port (`skills.persist.SkillStore`).

Each method runs in its own short transaction. Returns plain dicts; does not
import `permit_trade_skills.skills`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from permit_trade_skills.discovery.registry import DetailSnapshotTable, PermitTable, SkillInferenceTable
from permit_trade_skills.store import transaction

PERMIT_COLUMNS = ("id", "jurisdiction_id", "permit_number", "status", "processing_state", "permit_class",
                  "permit_class_mapped", "permit_type_mapped", "permit_type_desc", "valuation", "issued_at",
                  "address", "city", "state", "zip_code", "description", "detail_url")
SNAPSHOT_COLUMNS = tuple(c.name for c in DetailSnapshotTable.__table__.columns)
INFERENCE_COLUMNS = tuple(c.name for c in SkillInferenceTable.__table__.columns)
WRITABLE = tuple(c for c in INFERENCE_COLUMNS if c not in ("id", "created_at", "reviewer", "reviewed_at",
                                                           "review_note"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(obj, columns: Sequence[str]) -> Dict[str, Any]:
    return {c: getattr(obj, c) for c in columns}


class SqlSkillStore:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url

    # ---- reads -------------------------------------------------------------

    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        P = PermitTable
        with transaction(self.db_url) as s:
            p = s.execute(select(P).where(P.jurisdiction_id == jurisdiction, P.permit_number == permit_number)
                          ).scalar_one_or_none()
            return _row(p, PERMIT_COLUMNS) if p is not None else None

    def select_permits(self, jurisdiction: str, *, state: Optional[str] = "DETAIL_READY",
                       status: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        P = PermitTable
        stmt = select(P).where(P.jurisdiction_id == jurisdiction)
        if state:
            stmt = stmt.where(P.processing_state == state)
        if status:
            stmt = stmt.where(P.status == status)
        stmt = stmt.order_by(P.issued_at.desc().nulls_last(), P.id.desc()).limit(limit)
        with transaction(self.db_url) as s:
            return [_row(p, PERMIT_COLUMNS) for p in s.execute(stmt).scalars()]

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]:
        T = DetailSnapshotTable
        with transaction(self.db_url) as s:
            snap = s.execute(select(T).where(T.permit_id == permit_id)
                             .order_by(T.fetched_at.desc(), T.id.desc()).limit(1)).scalar_one_or_none()
            return _row(snap, SNAPSHOT_COLUMNS) if snap is not None else None

    def snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            snap = s.get(DetailSnapshotTable, snapshot_id)
            return _row(snap, SNAPSHOT_COLUMNS) if snap is not None else None

    def find_inference(self, permit_id: int, input_cache_key: str) -> Optional[Dict[str, Any]]:
        T = SkillInferenceTable
        with transaction(self.db_url) as s:
            row = s.execute(select(T).where(T.permit_id == permit_id, T.input_cache_key == input_cache_key)
                            ).scalar_one_or_none()
            return _row(row, INFERENCE_COLUMNS) if row is not None else None

    def latest_inference(self, permit_id: int) -> Optional[Dict[str, Any]]:
        T = SkillInferenceTable
        with transaction(self.db_url) as s:
            row = s.execute(select(T).where(T.permit_id == permit_id)
                            .order_by(T.created_at.desc(), T.id.desc()).limit(1)).scalar_one_or_none()
            return _row(row, INFERENCE_COLUMNS) if row is not None else None

    # ---- writes ------------------------------------------------------------

    def save_inference(self, row: Dict[str, Any]) -> Tuple[int, bool]:
        T = SkillInferenceTable.__table__
        values = {k: v for k, v in row.items() if k in WRITABLE}
        values["created_at"] = _utcnow()
        stmt = (pg_insert(T).values(**values)
                .on_conflict_do_nothing(index_elements=["permit_id", "input_cache_key"],
                                        index_where=text("input_cache_key IS NOT NULL"))
                .returning(T.c.id))
        with transaction(self.db_url) as s:
            new_id = s.execute(stmt).scalar_one_or_none()
            if new_id is not None:
                return new_id, True
            existing = s.execute(select(T.c.id).where(T.c.permit_id == values["permit_id"],
                                                      T.c.input_cache_key == values.get("input_cache_key"))
                                 ).scalar_one()
            return existing, False

    def mark_skills_state(self, permit_id: int, to_state: str, from_states: Tuple[str, ...],
                          error: Optional[Dict[str, Any]] = None) -> Optional[str]:
        P = PermitTable
        with transaction(self.db_url) as s:
            n = s.execute(
                update(P).where(P.id == permit_id, P.processing_state.in_(list(from_states)))
                .values(processing_state=to_state, state_updated_at=_utcnow(), state_error=error)
                .execution_options(synchronize_session=False)
            ).rowcount
        return to_state if n else None

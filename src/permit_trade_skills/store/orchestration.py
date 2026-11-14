"""SQL implementation of the orchestration stage's persistence port (`orchestration.ports.OrchestrationStore`).

Each method runs in its own short transaction (`apply_review` updates the
inference and the permit in one). Returns plain dicts; does not import
`permit_trade_skills.orchestration`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import Boolean, and_, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from permit_trade_skills.discovery.registry import (
    DetailSnapshotTable,
    JurisdictionTable,
    OrchestrationEventTable,
    PermitTable,
    PipelineRunTable,
    RetryBacklogTable,
    SkillInferenceTable,
)
from permit_trade_skills.store import transaction

EVENT_COLUMNS = tuple(c.name for c in OrchestrationEventTable.__table__.columns)
RUN_COLUMNS = tuple(c.name for c in PipelineRunTable.__table__.columns)
BACKLOG_COLUMNS = tuple(c.name for c in RetryBacklogTable.__table__.columns)
INFERENCE_COLUMNS = tuple(c.name for c in SkillInferenceTable.__table__.columns)
PERMIT_COLUMNS = ("id", "permit_number", "status", "processing_state", "state_error", "issued_at")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(obj, columns: Sequence[str]) -> Dict[str, Any]:
    return {c: getattr(obj, c) for c in columns}


class SqlOrchestrationStore:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url

    # ---- events / runs -----------------------------------------------------

    def add_event(self, event: Dict[str, Any]) -> None:
        values = {k: v for k, v in event.items() if k in EVENT_COLUMNS and k != "id"}
        values.setdefault("created_at", _utcnow())
        with transaction(self.db_url) as s:
            s.execute(pg_insert(OrchestrationEventTable.__table__).values(**values))

    def run_events(self, run_id: str) -> List[Dict[str, Any]]:
        E = OrchestrationEventTable
        with transaction(self.db_url) as s:
            return [_row(e, EVENT_COLUMNS) for e in
                    s.execute(select(E).where(E.run_id == run_id).order_by(E.id)).scalars()]

    def create_pipeline_run(self, run_id: str, jurisdiction: str, trigger: str, params: Dict[str, Any]) -> None:
        with transaction(self.db_url) as s:
            s.execute(pg_insert(PipelineRunTable.__table__).values(
                run_id=run_id, jurisdiction_id=jurisdiction, trigger=trigger, params=params, status="running",
                started_at=_utcnow()))

    def finish_pipeline_run(self, run_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        with transaction(self.db_url) as s:
            run = s.get(PipelineRunTable, run_id)
            for key in ("status", "planned", "completed", "failed", "skipped", "queued", "llm_calls",
                        "duration_ms", "error"):
                setattr(run, key, report.get(key))
            run.tokens = report.get("tokens") or {}
            run.est_cost = report.get("est_cost")
            run.finished_at = _utcnow()
            s.flush()
            return _row(run, RUN_COLUMNS)

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            run = s.get(PipelineRunTable, run_id)
            return _row(run, RUN_COLUMNS) if run is not None else None

    def recent_pipeline_runs(self, jurisdiction: Optional[str], limit: int) -> List[Dict[str, Any]]:
        R = PipelineRunTable
        stmt = select(R).order_by(R.started_at.desc()).limit(limit)
        if jurisdiction:
            stmt = stmt.where(R.jurisdiction_id == jurisdiction)
        with transaction(self.db_url) as s:
            return [_row(r, RUN_COLUMNS) for r in s.execute(stmt).scalars()]

    # ---- permits and the durable queue ------------------------------------

    def active_jurisdictions(self) -> List[str]:
        J = JurisdictionTable
        with transaction(self.db_url) as s:
            return list(s.execute(select(J.id).where(J.is_active.is_(True)).order_by(J.id)).scalars())

    def eligible_permits(self, jurisdiction: str, *, statuses: Sequence[str], states: Sequence[str],
                         since: Optional[datetime], permit_numbers: Optional[Sequence[str]], limit: int,
                         now: datetime, ignore_backlog: bool = False) -> List[Dict[str, Any]]:
        P, B = PermitTable, RetryBacklogTable
        retryable = func.coalesce(cast(P.state_error["retryable"].astext, Boolean), True)
        stmt = (select(P.id, P.permit_number, P.status, P.processing_state, P.state_error, P.issued_at,
                       B.stage, B.attempts, B.next_retry_at, B.exhausted)
                .outerjoin(B, B.permit_id == P.id)
                .where(P.jurisdiction_id == jurisdiction, P.status.in_(list(statuses)),
                       P.processing_state.in_(list(states)),
                       ~and_(P.processing_state == "SKILLS_FAILED", retryable.is_(False))))
        if not ignore_backlog:
            stmt = stmt.where(or_(B.permit_id.is_(None),
                                  and_(B.exhausted.is_(False),
                                       or_(B.next_retry_at.is_(None), B.next_retry_at <= now))))
        if since:
            stmt = stmt.where(P.issued_at >= since)
        if permit_numbers:
            stmt = stmt.where(P.permit_number.in_(list(permit_numbers)))
        stmt = stmt.order_by(P.issued_at.desc().nulls_last(), P.id.desc()).limit(limit)
        with transaction(self.db_url) as s:
            return [dict(r._mapping) for r in s.execute(stmt)]

    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        P = PermitTable
        with transaction(self.db_url) as s:
            p = s.execute(select(P).where(P.jurisdiction_id == jurisdiction, P.permit_number == permit_number)
                          ).scalar_one_or_none()
            return _row(p, PERMIT_COLUMNS) if p is not None else None

    def permit_state(self, permit_id: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        P = PermitTable
        with transaction(self.db_url) as s:
            row = s.execute(select(P.processing_state, P.state_error).where(P.id == permit_id)).first()
            return (row[0], row[1]) if row else (None, None)

    def advance_state(self, permit_id: int, from_state: str, to_state: str,
                      error: Optional[Dict[str, Any]] = None) -> bool:
        P = PermitTable
        with transaction(self.db_url) as s:
            n = s.execute(update(P).where(P.id == permit_id, P.processing_state == from_state)
                          .values(processing_state=to_state, state_updated_at=_utcnow(), state_error=error)
                          .execution_options(synchronize_session=False)).rowcount
        return n == 1

    def backlog(self, permit_id: int) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            row = s.get(RetryBacklogTable, permit_id)
            return _row(row, BACKLOG_COLUMNS) if row is not None else None

    def upsert_backlog(self, permit_id: int, row: Dict[str, Any]) -> None:
        values = {k: v for k, v in row.items() if k in BACKLOG_COLUMNS and k not in ("permit_id", "updated_at")}
        values = {**values, "updated_at": _utcnow()}
        stmt = pg_insert(RetryBacklogTable.__table__).values(permit_id=permit_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["permit_id"], set_={k: stmt.excluded[k] for k in values})
        with transaction(self.db_url) as s:
            s.execute(stmt)

    def clear_backlog(self, permit_id: int) -> None:
        with transaction(self.db_url) as s:
            s.execute(RetryBacklogTable.__table__.delete().where(RetryBacklogTable.permit_id == permit_id))

    # ---- review ------------------------------------------------------------

    def _latest_ids(self):
        T = SkillInferenceTable
        ranked = select(T.id, func.row_number().over(partition_by=T.permit_id,
                                                     order_by=(T.created_at.desc(), T.id.desc())).label("rn")
                        ).subquery()
        return select(ranked.c.id).where(ranked.c.rn == 1)

    def review_queue(self, jurisdiction: str, *, states: Optional[Sequence[str]],
                     review_statuses: Optional[Sequence[str]], confidence: Optional[str],
                     limit: int) -> List[Dict[str, Any]]:
        P, T = PermitTable, SkillInferenceTable
        stmt = (select(P.id.label("permit_id"), P.permit_number, P.status, P.processing_state,
                       T.id.label("inference_id"), T.overall_confidence, T.skills, T.dropped_skills, T.assumptions,
                       T.review_status, T.proposed_by, T.proposed_decision, T.review_note, T.reviewer,
                       T.created_at)
                .join(T, T.permit_id == P.id)
                .where(P.jurisdiction_id == jurisdiction, T.id.in_(self._latest_ids())))
        if states:
            stmt = stmt.where(P.processing_state.in_(list(states)))
        if review_statuses:
            stmt = stmt.where(T.review_status.in_(list(review_statuses)))
        if confidence:
            stmt = stmt.where(T.overall_confidence == confidence)
        stmt = stmt.order_by(T.created_at, T.id).limit(limit)
        with transaction(self.db_url) as s:
            return [dict(r._mapping) for r in s.execute(stmt)]

    def latest_inference(self, permit_id: int) -> Optional[Dict[str, Any]]:
        T = SkillInferenceTable
        with transaction(self.db_url) as s:
            row = s.execute(select(T).where(T.permit_id == permit_id)
                            .order_by(T.created_at.desc(), T.id.desc()).limit(1)).scalar_one_or_none()
            return _row(row, INFERENCE_COLUMNS) if row is not None else None

    def snapshot_summary(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            snap = s.get(DetailSnapshotTable, snapshot_id)
            return _row(snap, ("id", "final_url", "fetch_strategy", "fetched_at", "content_hash", "degraded")) \
                if snap is not None else None

    def apply_review(self, *, inference_id: int, permit_id: int, review_status: str,
                     reviewer: Optional[str], note: Optional[str], proposal: Optional[Tuple[str, str]],
                     transition: Optional[Tuple[str, str]]) -> bool:
        T, P = SkillInferenceTable, PermitTable
        values: Dict[str, Any] = {"review_status": review_status, "review_note": note, "reviewer": reviewer,
                                  "reviewed_at": _utcnow() if reviewer else None}
        if proposal is not None:
            values["proposed_by"], values["proposed_decision"] = proposal
        with transaction(self.db_url) as s:
            if transition is not None:
                n = s.execute(update(P).where(P.id == permit_id, P.processing_state == transition[0])
                              .values(processing_state=transition[1], state_updated_at=_utcnow(), state_error=None)
                              .execution_options(synchronize_session=False)).rowcount
                if n != 1:
                    s.rollback()
                    return False
            n = s.execute(update(T).where(T.id == inference_id, T.permit_id == permit_id,
                                          T.review_status.in_(("PENDING", "NEEDS_HUMAN")))
                          .values(**values).execution_options(synchronize_session=False)).rowcount
            if n != 1:
                s.rollback()
                return False
        return True

    # ---- dashboard ---------------------------------------------------------

    def cohort_status(self, jurisdiction: str, since: datetime) -> Dict[str, Any]:
        P, E = PermitTable, OrchestrationEventTable
        touched = and_(P.jurisdiction_id == jurisdiction, P.state_updated_at >= since)
        reason = P.state_error["error"].astext
        with transaction(self.db_url) as s:
            by_state = dict(s.execute(select(P.processing_state, func.count()).where(touched)
                                      .group_by(P.processing_state)).all())
            failures = [{"state": st, "error": err, "count": n} for st, err, n in s.execute(
                select(P.processing_state, reason, func.count())
                .where(touched, P.processing_state.in_(("DETAIL_FAILED", "SKILLS_FAILED", "SKILLS_SKIPPED")))
                .group_by(P.processing_state, reason).order_by(func.count().desc()))]
            latency = {ev: {"count": n, "p50_ms": p50, "p95_ms": p95} for ev, n, p50, p95 in s.execute(
                select(E.event, func.count(),
                       func.percentile_cont(0.5).within_group(E.latency_ms),
                       func.percentile_cont(0.95).within_group(E.latency_ms))
                .join(P, P.id == E.permit_id)
                .where(P.jurisdiction_id == jurisdiction, E.created_at >= since, E.event.in_(("step.detail", "step.infer")))
                .group_by(E.event))}
            runs = s.execute(select(func.count(), func.coalesce(func.sum(PipelineRunTable.llm_calls), 0))
                             .where(PipelineRunTable.jurisdiction_id == jurisdiction,
                                    PipelineRunTable.started_at >= since)).one()
        return {"by_state": by_state, "total": sum(by_state.values()), "failures": failures,
                "step_latency": {k: {"count": v["count"],
                                     "p50_ms": round(v["p50_ms"]) if v["p50_ms"] is not None else None,
                                     "p95_ms": round(v["p95_ms"]) if v["p95_ms"] is not None else None}
                                 for k, v in latency.items()},
                "pipeline_runs": runs[0], "llm_calls": int(runs[1])}

"""SQL implementation of the evaluation stage's persistence port (`eval.ports.EvalStore`).

Each method runs in its own short transaction and returns plain dicts. Does
not import `permit_trade_skills.eval`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from permit_trade_skills.discovery.registry import (
    BuildGateTable,
    DetailSnapshotTable,
    DriftAlertTable,
    EvalResultTable,
    JurisdictionTable,
    LabelTable,
    OrchestrationEventTable,
    PermitTable,
    PipelineRunTable,
    SkillInferenceTable,
    SyncMetadataTable,
)
from permit_trade_skills.store import transaction
from permit_trade_skills.store.repos import PermitRepository

PERMIT_COLUMNS = ("id", "jurisdiction_id", "permit_number", "status", "status_raw", "processing_state",
                  "permit_class", "permit_class_mapped", "permit_type_mapped", "permit_type_desc", "valuation",
                  "issued_at", "address", "city", "state", "zip_code", "description", "detail_url", "raw_row",
                  "raw_hash", "created_at", "updated_at")
SNAPSHOT_COLUMNS = tuple(c.name for c in DetailSnapshotTable.__table__.columns)
INFERENCE_COLUMNS = tuple(c.name for c in SkillInferenceTable.__table__.columns)
RESULT_COLUMNS = tuple(c.name for c in EvalResultTable.__table__.columns)
LABEL_COLUMNS = tuple(c.name for c in LabelTable.__table__.columns)
GATE_COLUMNS = tuple(c.name for c in BuildGateTable.__table__.columns)
ALERT_COLUMNS = tuple(c.name for c in DriftAlertTable.__table__.columns)
EVENT_COLUMNS = tuple(c.name for c in OrchestrationEventTable.__table__.columns)
JURISDICTION_COLUMNS = ("id", "name", "county", "state", "platform_type", "base_url", "api_endpoint",
                        "dataset_id", "field_map", "status_map", "detail_strategy", "is_active")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(obj, columns: Sequence[str]) -> Dict[str, Any]:
    return {c: getattr(obj, c) for c in columns}


def _num(value: Any) -> Any:
    return float(value) if value is not None and not isinstance(value, (int, float, bool)) else value


def _result(obj) -> Dict[str, Any]:
    row = _row(obj, RESULT_COLUMNS)
    for key in ("value", "threshold"):
        row[key] = _num(row[key])
    return row


class SqlEvalStore:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url

    # ---- reference data ----------------------------------------------------

    def active_jurisdictions(self) -> List[str]:
        J = JurisdictionTable
        with transaction(self.db_url) as s:
            return list(s.execute(select(J.id).where(J.is_active.is_(True)).order_by(J.id)).scalars())

    def jurisdiction(self, slug: str) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            j = s.get(JurisdictionTable, slug)
            return _row(j, JURISDICTION_COLUMNS) if j is not None else None

    def permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        P = PermitTable
        with transaction(self.db_url) as s:
            p = s.execute(select(P).where(P.jurisdiction_id == jurisdiction, P.permit_number == permit_number)
                          ).scalar_one_or_none()
            return _row(p, PERMIT_COLUMNS) if p is not None else None

    def snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        with transaction(self.db_url) as s:
            snap = s.get(DetailSnapshotTable, snapshot_id)
            return _row(snap, SNAPSHOT_COLUMNS) if snap is not None else None

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]:
        T = DetailSnapshotTable
        with transaction(self.db_url) as s:
            snap = s.execute(select(T).where(T.permit_id == permit_id)
                             .order_by(T.fetched_at.desc(), T.id.desc()).limit(1)).scalar_one_or_none()
            return _row(snap, SNAPSHOT_COLUMNS) if snap is not None else None

    def recent_snapshots(self, jurisdiction: str, *, limit: Optional[int] = None, strategy: Optional[str] = None,
                         since: Optional[datetime] = None, latest_only: bool = True) -> List[Dict[str, Any]]:
        T, P = DetailSnapshotTable, PermitTable
        stmt = (select(T, P.permit_number, P.address, P.status_raw, P.raw_row, P.issued_at)
                .join(P, P.id == T.permit_id).where(P.jurisdiction_id == jurisdiction))
        if latest_only:
            newest = (select(func.max(T.id)).where(T.permit_id == P.id).correlate(P).scalar_subquery())
            stmt = stmt.where(T.id == newest)
        if strategy:
            stmt = stmt.where(T.fetch_strategy == strategy)
        if since:
            stmt = stmt.where(T.fetched_at >= since)
        stmt = stmt.order_by(T.fetched_at.desc(), T.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        with transaction(self.db_url) as s:
            out = []
            for snap, number, address, status_raw, raw_row, issued_at in s.execute(stmt):
                out.append({**_row(snap, SNAPSHOT_COLUMNS), "permit_number": number, "address": address,
                            "status_raw": status_raw, "raw_row": raw_row, "issued_at": issued_at})
            return out

    # ---- inferences, events ------------------------------------------------

    def accepted_inferences(self, jurisdiction: str, *, since: Optional[datetime] = None,
                            limit: Optional[int] = None) -> List[Dict[str, Any]]:
        T, P, D = SkillInferenceTable, PermitTable, DetailSnapshotTable
        stmt = (select(T, P.permit_number, P.detail_url, P.processing_state, D.content_text, D.final_url,
                       D.fetch_strategy, D.degraded.label("snapshot_degraded"))
                .join(P, P.id == T.permit_id).outerjoin(D, D.id == T.input_snapshot_id)
                .where(P.jurisdiction_id == jurisdiction))
        if since:
            stmt = stmt.where(T.created_at >= since)
        stmt = stmt.order_by(T.created_at.desc(), T.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        with transaction(self.db_url) as s:
            out = []
            for inf, number, url, state, content, final_url, strategy, snap_degraded in s.execute(stmt):
                out.append({**_row(inf, INFERENCE_COLUMNS), "permit_number": number, "detail_url": url,
                            "processing_state": state, "snapshot_content_text": content,
                            "snapshot_final_url": final_url, "snapshot_fetch_strategy": strategy,
                            "snapshot_degraded": snap_degraded})
            return out

    def agent_runs(self, *, since: Optional[datetime] = None, limit: Optional[int] = None) -> List[str]:
        E = OrchestrationEventTable
        stmt = select(E.run_id, func.max(E.id).label("last")).where(E.event == "agent.finish")
        if since:
            stmt = stmt.where(E.created_at >= since)
        stmt = stmt.group_by(E.run_id).order_by(text("last DESC"))
        if limit:
            stmt = stmt.limit(limit)
        with transaction(self.db_url) as s:
            return [r[0] for r in s.execute(stmt)]

    def run_events(self, run_id: str) -> List[Dict[str, Any]]:
        E = OrchestrationEventTable
        with transaction(self.db_url) as s:
            return [_row(e, EVENT_COLUMNS) for e in
                    s.execute(select(E).where(E.run_id == run_id).order_by(E.id)).scalars()]

    def illegal_transitions(self, *, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        E, P = OrchestrationEventTable, PermitTable
        stmt = (select(E, P.permit_number).outerjoin(P, P.id == E.permit_id)
                .where(E.error_code == "ILLEGAL_TRANSITION"))
        if since:
            stmt = stmt.where(E.created_at >= since)
        with transaction(self.db_url) as s:
            return [{**_row(e, EVENT_COLUMNS), "permit_number": n} for e, n in s.execute(stmt.order_by(E.id))]

    def self_approvals(self, jurisdiction: str) -> List[Dict[str, Any]]:
        """Approve/reject decisions without a human reviewer, and approved permits without an approved row."""
        T, P = SkillInferenceTable, PermitTable
        decided = T.review_status.in_(("APPROVED", "REJECTED"))
        no_human = or_(T.reviewer.is_(None), func.lower(func.trim(T.reviewer)).in_(("", "agent")))
        out: List[Dict[str, Any]] = []
        with transaction(self.db_url) as s:
            for inf_id, number, status, reviewer, proposed_by in s.execute(
                    select(T.id, P.permit_number, T.review_status, T.reviewer, T.proposed_by)
                    .join(P, P.id == T.permit_id)
                    .where(P.jurisdiction_id == jurisdiction, decided,
                           or_(no_human, and_(T.proposed_by == "agent", T.reviewer.is_(None))))):
                out.append({"type": "decision_without_human", "inference_id": inf_id, "permit_number": number,
                            "review_status": status, "reviewer": reviewer, "proposed_by": proposed_by})
            approved = select(T.id).where(T.permit_id == P.id, T.review_status == "APPROVED").exists()
            for pid, number in s.execute(select(P.id, P.permit_number).where(
                    P.jurisdiction_id == jurisdiction, P.processing_state == "SKILLS_APPROVED", ~approved)):
                out.append({"type": "approved_state_without_approval", "permit_id": pid, "permit_number": number})
        return out

    # ---- results -------------------------------------------------------------

    def cached_payload(self, cache_key: str) -> Optional[Dict[str, Any]]:
        R = EvalResultTable
        with transaction(self.db_url) as s:
            row = s.execute(select(R.payload).where(R.cache_key == cache_key, R.payload.isnot(None))
                            .order_by(R.id.desc()).limit(1)).first()
            return row[0] if row else None

    def save_results(self, rows: Sequence[Dict[str, Any]]) -> None:
        if not rows:
            return
        now = _utcnow()
        # executemany needs one key set: every row gets every column
        values = [{**{k: r.get(k) for k in RESULT_COLUMNS if k != "id"}, "created_at": r.get("created_at") or now}
                  for r in rows]
        with transaction(self.db_url) as s:
            s.execute(pg_insert(EvalResultTable.__table__), values)

    def latest_summary(self, suite: str, *, build_id: Optional[str] = None,
                       jurisdiction: Optional[str] = None) -> Optional[Dict[str, Any]]:
        R = EvalResultTable
        stmt = select(R).where(R.suite == suite, R.metric == func.to_jsonb(text("'summary'::text")))
        if build_id:
            stmt = stmt.where(R.build_id == build_id)
        if jurisdiction:
            stmt = stmt.where(R.payload["jurisdiction"].astext == jurisdiction)
        with transaction(self.db_url) as s:
            row = s.execute(stmt.order_by(R.created_at.desc(), R.id.desc()).limit(1)).scalar_one_or_none()
            return _result(row) if row is not None else None

    def summaries(self, suite: Optional[str] = None, *, since: Optional[datetime] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        R = EvalResultTable
        stmt = select(R).where(R.metric == func.to_jsonb(text("'summary'::text")))
        if suite:
            stmt = stmt.where(R.suite == suite)
        if since:
            stmt = stmt.where(R.created_at >= since)
        with transaction(self.db_url) as s:
            return [_result(r) for r in s.execute(stmt.order_by(R.created_at.desc(), R.id.desc()).limit(limit))
                    .scalars()]

    def items(self, suite: str, *, run_id: Optional[str] = None, since: Optional[datetime] = None,
              failed_only: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        R = EvalResultTable
        stmt = select(R).where(R.suite == suite, R.metric == func.to_jsonb(text("'item'::text")))
        if run_id:
            stmt = stmt.where(R.run_id == run_id)
        if since:
            stmt = stmt.where(R.created_at >= since)
        if failed_only:
            stmt = stmt.where(R.passed.is_(False))
        stmt = stmt.order_by(R.created_at.desc(), R.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        with transaction(self.db_url) as s:
            return [_result(r) for r in s.execute(stmt).scalars()]

    # ---- labels ----------------------------------------------------------------

    def add_label(self, row: Dict[str, Any], *, replaces: Optional[int] = None) -> int:
        """Insert a label; `replaces` (a current row) is superseded by it in the same transaction."""
        L = LabelTable
        values = {k: v for k, v in row.items() if k in LABEL_COLUMNS and k != "id"}
        values.setdefault("created_at", _utcnow())
        with transaction(self.db_url) as s:
            if replaces is not None:   # retire first: the current-label unique index allows one live row
                s.execute(update(L).where(L.id == replaces).values(superseded_by=replaces))
            new_id = s.execute(pg_insert(L.__table__).values(**values).returning(L.id)).scalar_one()
            if replaces is not None:
                s.execute(update(L).where(L.id == replaces).values(superseded_by=new_id))
            return new_id

    def reviewers(self, permit_id: int) -> Set[str]:
        T = SkillInferenceTable
        with transaction(self.db_url) as s:
            return {r for r in s.execute(select(T.reviewer).where(T.permit_id == permit_id,
                                                                  T.reviewer.isnot(None))).scalars()}

    def current_labels(self, jurisdiction: str, kind: str, item_key: Optional[str] = None,
                       role: Optional[str] = None) -> List[Dict[str, Any]]:
        L = LabelTable
        stmt = select(L).where(L.jurisdiction_id == jurisdiction, L.kind == kind, L.superseded_by.is_(None))
        if item_key:
            stmt = stmt.where(L.item_key == item_key)
        if role:
            stmt = stmt.where(L.role == role)
        with transaction(self.db_url) as s:
            return [_row(r, LABEL_COLUMNS) for r in s.execute(stmt.order_by(L.item_key, L.id)).scalars()]

    def supersede_label(self, label_id: int, by_id: Optional[int]) -> None:
        """Retire a row: superseded_by = the replacement, or the row's own id when nothing replaces it."""
        with transaction(self.db_url) as s:
            s.execute(update(LabelTable).where(LabelTable.id == label_id)
                      .values(superseded_by=by_id if by_id is not None else label_id))

    # ---- gate, alerts ------------------------------------------------------------

    def save_gate(self, row: Dict[str, Any]) -> int:
        values = {k: v for k, v in row.items() if k in GATE_COLUMNS and k != "id"}
        values.setdefault("created_at", _utcnow())
        with transaction(self.db_url) as s:
            return s.execute(pg_insert(BuildGateTable.__table__).values(**values)
                             .returning(BuildGateTable.id)).scalar_one()

    def latest_gate(self, build_id: str) -> Optional[Dict[str, Any]]:
        G = BuildGateTable
        with transaction(self.db_url) as s:
            row = s.execute(select(G).where(G.build_id == build_id).order_by(G.created_at.desc(), G.id.desc())
                            .limit(1)).scalar_one_or_none()
            return _row(row, GATE_COLUMNS) if row is not None else None

    def sign_off_gate(self, gate_id: int, by: str, note: str) -> bool:
        G = BuildGateTable
        with transaction(self.db_url) as s:
            n = s.execute(update(G).where(G.id == gate_id, G.status == "CONDITIONAL", G.signed_off_by.is_(None))
                          .values(signed_off_by=by, signed_off_at=_utcnow(), sign_off_note=note)).rowcount
        return n == 1

    def add_alert(self, row: Dict[str, Any]) -> int:
        values = {k: v for k, v in row.items() if k in ALERT_COLUMNS and k != "id"}
        values.setdefault("created_at", _utcnow())
        with transaction(self.db_url) as s:
            return s.execute(pg_insert(DriftAlertTable.__table__).values(**values)
                             .returning(DriftAlertTable.id)).scalar_one()

    def alerts(self, *, since: Optional[datetime] = None, detector: Optional[str] = None,
               open_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        A = DriftAlertTable
        stmt = select(A)
        if since:
            stmt = stmt.where(A.created_at >= since)
        if detector:
            stmt = stmt.where(A.detector == detector)
        if open_only:
            stmt = stmt.where(A.acknowledged_at.is_(None))
        with transaction(self.db_url) as s:
            return [_row(a, ALERT_COLUMNS) for a in
                    s.execute(stmt.order_by(A.created_at.desc(), A.id.desc()).limit(limit)).scalars()]

    def mark_alert(self, alert_id: int, *, notified: Optional[bool] = None,
                   acknowledged_by: Optional[str] = None) -> bool:
        values: Dict[str, Any] = {}
        if notified is not None:
            values["notified"] = notified
        if acknowledged_by:
            values.update(acknowledged_by=acknowledged_by, acknowledged_at=_utcnow())
        if not values:
            return False
        with transaction(self.db_url) as s:
            return s.execute(update(DriftAlertTable).where(DriftAlertTable.id == alert_id)
                             .values(**values)).rowcount == 1

    # ---- monitoring queries ------------------------------------------------------

    def confidence_counts(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, Any]:
        T, P = SkillInferenceTable, PermitTable
        where = (P.jurisdiction_id == jurisdiction, T.created_at >= start, T.created_at < end)
        with transaction(self.db_url) as s:
            counts = dict(s.execute(select(T.overall_confidence, func.count()).join(P, P.id == T.permit_id)
                                    .where(*where).group_by(T.overall_confidence)).all())
            models = {f"{m}@{v}": n for m, v, n in s.execute(
                select(T.model, T.prompt_version, func.count()).join(P, P.id == T.permit_id)
                .where(*where).group_by(T.model, T.prompt_version))}
        return {"counts": {k: int(counts.get(k, 0)) for k in ("high", "medium", "low")}, "models": models}

    def unknown_status_share(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, int]:
        P = PermitTable
        written = or_(and_(P.created_at >= start, P.created_at < end), and_(P.updated_at >= start, P.updated_at < end))
        with transaction(self.db_url) as s:
            total, unknown = s.execute(select(func.count(), func.count().filter(P.status == "UNKNOWN"))
                                       .where(P.jurisdiction_id == jurisdiction, written)).one()
        return {"unknown": int(unknown), "total": int(total)}

    def degraded_share(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, int]:
        T, P = DetailSnapshotTable, PermitTable
        with transaction(self.db_url) as s:
            total, degraded = s.execute(
                select(func.count(), func.count().filter(T.degraded.is_(True))).join(P, P.id == T.permit_id)
                .where(P.jurisdiction_id == jurisdiction, T.fetched_at >= start, T.fetched_at < end)).one()
        return {"degraded": int(degraded), "total": int(total)}

    def slo_rows(self, jurisdiction: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        T, P = SkillInferenceTable, PermitTable
        first = select(func.min(T.created_at)).where(T.permit_id == P.id).correlate(P).scalar_subquery()
        stmt = (select(P.permit_number, P.created_at, P.issued_at, first.label("first_inference_at"))
                .where(P.jurisdiction_id == jurisdiction, P.status == "ISSUED", P.created_at >= start,
                       P.created_at < end).order_by(P.created_at, P.id))
        with transaction(self.db_url) as s:
            return [dict(r._mapping) for r in s.execute(stmt)]

    def count_query(self, query: Any) -> int:
        with transaction(self.db_url) as s:
            return sum(PermitRepository(s).status_counts(query).values())

    def permit_numbers_present(self, jurisdiction: str, numbers: Iterable[str]) -> Set[str]:
        P = PermitTable
        numbers = list(dict.fromkeys(numbers))
        found: Set[str] = set()
        with transaction(self.db_url) as s:
            for i in range(0, len(numbers), 10_000):
                chunk = numbers[i:i + 10_000]
                found.update(s.execute(select(P.permit_number).where(P.jurisdiction_id == jurisdiction,
                                                                     P.permit_number.in_(chunk))).scalars())
        return found

    def status_distribution(self, jurisdiction: str) -> Dict[str, int]:
        P = PermitTable
        with transaction(self.db_url) as s:
            return {k: int(v) for k, v in s.execute(select(P.status, func.count())
                                                    .where(P.jurisdiction_id == jurisdiction)
                                                    .group_by(P.status)).all()}

    def latest_sync(self, jurisdiction: str) -> Optional[Dict[str, Any]]:
        M = SyncMetadataTable
        cols = tuple(c.name for c in M.__table__.columns)
        with transaction(self.db_url) as s:
            row = s.execute(select(M).where(M.jurisdiction_id == jurisdiction, M.error.is_(None))
                            .order_by(M.started_at.desc()).limit(1)).scalar_one_or_none()
            return _row(row, cols) if row is not None else None

    def metric_counts(self, jurisdiction: Optional[str]) -> Dict[str, Any]:
        P, D, T, E, R = PermitTable, DetailSnapshotTable, SkillInferenceTable, OrchestrationEventTable, PipelineRunTable
        pj = (P.jurisdiction_id == jurisdiction) if jurisdiction else text("true")
        with transaction(self.db_url) as s:
            states = {k: int(v) for k, v in s.execute(select(P.processing_state, func.count()).where(pj)
                                                      .group_by(P.processing_state)).all()}
            statuses = {k: int(v) for k, v in s.execute(select(P.status, func.count()).where(pj)
                                                        .group_by(P.status)).all()}
            snaps = s.execute(select(func.count(), func.count().filter(D.degraded.is_(True)))
                              .join(P, P.id == D.permit_id).where(pj)).one()
            detail_failed = s.execute(select(func.count()).where(pj, P.processing_state == "DETAIL_FAILED")).scalar_one()
            confidence = {k or "none": int(v) for k, v in s.execute(
                select(T.overall_confidence, func.count()).join(P, P.id == T.permit_id).where(pj)
                .group_by(T.overall_confidence)).all()}
            grounding = s.execute(select(func.count()).where(
                pj, P.state_error["error"].astext == "GROUNDING_FAILED")).scalar_one()
            latency = {ev: (p50, p95) for ev, p50, p95 in s.execute(
                select(E.event, func.percentile_cont(0.5).within_group(E.latency_ms),
                       func.percentile_cont(0.95).within_group(E.latency_ms))
                .where(E.event.in_(("step.detail", "step.infer")), E.latency_ms.isnot(None)).group_by(E.event))}
            tokens = s.execute(select(R.tokens).where(
                R.jurisdiction_id == jurisdiction if jurisdiction else text("true"))).scalars().all()
            last_agent = s.execute(select(E.result_json).where(E.event == "agent.finish")
                                   .order_by(E.id.desc()).limit(1)).scalar_one_or_none()
        token_sum = {"input": 0, "output": 0}
        for t in tokens:
            for k in token_sum:
                token_sum[k] += int((t or {}).get(k, 0) or 0)
        return {"states": states, "statuses": statuses, "snapshots": int(snaps[0]), "snapshots_degraded": int(snaps[1]),
                "detail_failed": int(detail_failed), "confidence": confidence, "grounding_failed": int(grounding),
                "latency": latency, "pipeline_tokens": token_sum,
                "last_agent_integrity": (last_agent or {}).get("integrity") if last_agent else None}

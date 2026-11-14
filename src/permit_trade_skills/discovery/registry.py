"""Database registry for managing jurisdictions."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Float,
    create_engine,
    Column,
    String,
    Integer,
    DateTime,
    Boolean,
    JSON,
    Numeric,
    Text,
    ForeignKey,
    Index,
    UniqueConstraint,
    exists,
    text,
)
from sqlalchemy.types import UserDefinedType
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.dialects.postgresql import JSONB

from permit_trade_skills.ingest.schemas import ProcessingState, Status

from permit_trade_skills.discovery.schemas import (
    PlatformType,
    DiscoveryStatus,
    DiscoveryDossier,
    DiscoveryCandidate,
    ProbeResult,
    Jurisdiction,
    JurisdictionConfig,
    DiscoveryRun,
)


Base = declarative_base()

# Server-side "now" for the naive-UTC DateTime columns used throughout the schema.
UTC_NOW = text("(now() AT TIME ZONE 'utc')")

PERMIT_STATUSES = tuple(s.value for s in Status)
PROCESSING_STATES = tuple(s.value for s in ProcessingState)
FETCH_STRATEGIES = ("soda_row", "redirect_chase", "playwright")
REVIEW_STATUSES = ("PENDING", "APPROVED", "REJECTED", "NEEDS_HUMAN")


def _in_check(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PgPoint(UserDefinedType):
    """Postgres built-in geometric ``point`` (no PostGIS needed)."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "POINT"


# Let reflection (alembic autogenerate, inspect) recognise the column type.
from sqlalchemy.dialects.postgresql.base import ischema_names as _pg_ischema_names  # noqa: E402

_pg_ischema_names.setdefault("point", PgPoint)


class JurisdictionTable(Base):
    """Database table for jurisdictions."""

    __tablename__ = "jurisdictions"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    state = Column(String, nullable=False)
    county = Column(String, nullable=True)
    platform_type = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    api_endpoint = Column(String, nullable=True)
    api_key = Column(String, nullable=True)
    dataset_id = Column(String, nullable=True)
    dataset_name = Column(String, nullable=True)
    config_version = Column(Integer, default=1, nullable=False)
    field_map = Column(JSONB, default=dict, nullable=False)
    status_map = Column(JSONB, default=dict, nullable=False)
    detail_strategy = Column(JSONB, default=dict, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("config_version >= 1", name="ck_jurisdictions_config_version"),
    )

    # Relationships
    discovery_runs = relationship("DiscoveryRunTable", back_populates="jurisdiction")
    status_maps = relationship("StatusMapTable", back_populates="jurisdiction")
    probes = relationship("ProbeTable", back_populates="jurisdiction")
    permits = relationship("PermitTable", back_populates="jurisdiction")
    versions = relationship("JurisdictionVersionTable", back_populates="jurisdiction",
                            passive_deletes=True)


class DiscoveryRunTable(Base):
    """Database table for discovery runs."""

    __tablename__ = "discovery_runs"

    id = Column(String, primary_key=True)
    # NULL until the run's dossier is registered (`discover` runs before the
    # jurisdiction exists; `register` links it). Since 0006.
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id"), nullable=True)
    search_query = Column(Text, nullable=False)
    search_parameters = Column(JSONB, default=dict, nullable=False)
    total_results = Column(Integer, default=0)
    candidates_found = Column(Integer, default=0)
    status = Column(String, default=DiscoveryStatus.PENDING.value, nullable=False)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    jurisdiction = relationship("JurisdictionTable", back_populates="discovery_runs")


class JurisdictionVersionTable(Base):
    """A jurisdiction's config as it was before an in-place update (Stage 01 R1, since 0006).

    `config` holds every config column of the replaced row except `api_key`
    (only `api_key_set` is kept). `created_at` is when it was replaced.
    """

    __tablename__ = "jurisdiction_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(
        String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False
    )
    config_version = Column(Integer, nullable=False)
    config = Column(JSONB, nullable=False)
    replaced_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)

    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "config_version", name="uq_jurisdiction_versions_version"),
    )

    jurisdiction = relationship("JurisdictionTable", back_populates="versions")


class StatusMapTable(Base):
    """Database table for status mappings."""

    __tablename__ = "status_maps"

    id = Column(String, primary_key=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id"), nullable=False)
    source_status = Column(String, nullable=False)
    target_status = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=True, server_default=UTC_NOW)

    jurisdiction = relationship("JurisdictionTable", back_populates="status_maps")


class ProbeTable(Base):
    """Database table for verification probes."""

    __tablename__ = "probes"

    id = Column(String, primary_key=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id"), nullable=False)
    probe_name = Column(String, nullable=False)
    success = Column(Boolean, nullable=False)
    detected_platform = Column(String, nullable=True)
    confidence = Column(Integer, nullable=True)
    details = Column(JSONB, default=dict, nullable=False)
    error_message = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow)

    jurisdiction = relationship("JurisdictionTable", back_populates="probes")



class PermitTable(Base):
    """Database table for canonical permit records."""
    
    __tablename__ = "permits"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(
        String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False
    )
    jurisdiction_slug = Column(String, nullable=False)
    permit_number = Column(String, nullable=False)
    
    permit_class = Column(String)
    permit_class_mapped = Column(String)
    permit_type_mapped = Column(String)
    permit_type_desc = Column(String)
    
    status_raw = Column(String)
    status = Column(String, nullable=False, default="UNKNOWN")
    
    issue_date = Column(DateTime)
    issued_at = Column(DateTime)
    applied_at = Column(DateTime)
    expires_at = Column(DateTime)
    completed_at = Column(DateTime)
    
    description = Column(Text)
    address = Column(String)
    city = Column(String)
    state = Column(String)
    zip_code = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)
    # Generated (lng, lat) point for GiST proximity queries; read-only.
    geo = Column(
        PgPoint(),
        Computed("CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL "
                 "THEN point(longitude, latitude) END", persisted=True),
    )
    
    valuation = Column(Float)
    work_class = Column(String)
    building_count = Column(Integer)
    housing_units = Column(Integer)
    housing_units_added = Column(Integer)
    housing_units_removed = Column(Integer)
    housing_category = Column(String)
    contractor_name = Column(String)
    applicant_name = Column(String)
    
    status_map_key = Column(String)

    # Semantic search embedding (all-MiniLM-L6-v2 -> 384 dims)
    description_embedding = Column(Vector(384), nullable=True)
    
    raw_row = Column(JSON, nullable=False)
    raw_hash = Column(String, nullable=False)
    row_id_source = Column(String)       # Socrata ":id"
    row_version_source = Column(String)  # Socrata ":version"
    source_updated_at = Column(DateTime)
    source_synced_at = Column(DateTime)
    # Per-row normalization warnings, e.g. [{"type": "unknown_status", ...}]
    warnings = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    
    detail_url = Column(String)
    detail_strategy = Column(String)
    detail_fetch_type = Column(String)
    
    run_id = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    synced_at = Column(DateTime)
    processing_state = Column(String, nullable=False, default="SYNCED")
    state_updated_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    state_error = Column(JSONB)

    __table_args__ = (
        # Upsert looks permits up by this pair; without it every lookup is a
        # full table scan (hours for a ~200k-row re-sync).
        Index("idx_permit_jurisdiction_number", "jurisdiction_id", "permit_number", unique=True),
        # Hot path: "issued permits in <jurisdiction> in the last N days"
        Index("idx_permits_slug_status_issued", "jurisdiction_slug", "status", text("issued_at DESC")),
        Index("idx_permits_slug_class", "jurisdiction_slug", "permit_class_mapped"),
        Index("idx_permits_slug_zip", "jurisdiction_slug", "zip_code"),
        Index("brin_permits_issued_at", "issued_at", postgresql_using="brin"),
        Index("brin_permits_applied_at", "applied_at", postgresql_using="brin"),
        Index("gin_permits_description_trgm", "description",
              postgresql_using="gin", postgresql_ops={"description": "gin_trgm_ops"}),
        Index("gist_permits_geo", "geo", postgresql_using="gist"),
        CheckConstraint("latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
                        name="ck_permits_latitude"),
        CheckConstraint("longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
                        name="ck_permits_longitude"),
        CheckConstraint(_in_check("status", PERMIT_STATUSES), name="ck_permits_status"),
        CheckConstraint(_in_check("processing_state", PROCESSING_STATES),
                        name="ck_permits_processing_state"),
    )
    
    jurisdiction = relationship("JurisdictionTable", back_populates="permits")
    status_history = relationship("StatusHistoryTable", back_populates="permit",
                                  passive_deletes=True)


class StatusHistoryTable(Base):
    """Append-only audit trail of permit status transitions (written by sync)."""
    
    __tablename__ = "status_history"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"), nullable=False)
    jurisdiction_id = Column(String, nullable=False)
    
    from_status = Column(String)
    to_status = Column(String, nullable=False)
    raw_status = Column(String)
    changed_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    changed_by = Column(String, default="sync")
    run_id = Column(String)
    notes = Column(Text)

    __table_args__ = (
        Index("idx_status_history_permit_changed", "permit_id", text("changed_at DESC")),
    )
    
    permit = relationship("PermitTable", back_populates="status_history")


class SyncMetadataTable(Base):
    """Metadata about sync runs."""
    
    __tablename__ = "sync_metadata"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(
        String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False
    )
    jurisdiction_slug = Column(String, nullable=False)
    run_id = Column(String, nullable=False)
    config_version = Column(Integer, nullable=False)
    mode = Column(String, nullable=False, server_default="full")
    
    pull_count = Column(Integer, nullable=False)
    inserted = Column(Integer, nullable=False)
    updated = Column(Integer, nullable=False)
    unchanged = Column(Integer, nullable=False)
    dropped = Column(Integer, nullable=False)
    
    warnings = Column(JSON)
    # {"FROM->TO": count} for status changes applied in this run
    status_transitions = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    duration_ms = Column(Integer, nullable=False)
    
    cursor_before = Column(String)
    cursor_after = Column(String)
    # Since 0007: high-water marks and the pulled-set fingerprint (Stage 02 P2)
    max_applied_at = Column(DateTime)
    max_synced_at = Column(DateTime)
    fingerprint = Column(String)
    
    error = Column(String)
    
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_sync_metadata_jurisdiction_started", "jurisdiction_id", text("started_at DESC")),
        CheckConstraint("mode IN ('full', 'incremental', 'dry_run')", name="ck_sync_metadata_mode"),
    )


class DetailSnapshotTable(Base):
    """Fetched permit detail pages (written by the detail-extraction stage)."""

    __tablename__ = "detail_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"), nullable=False)
    final_url = Column(Text, nullable=False)
    fetch_strategy = Column(String, nullable=False)
    content_text = Column(Text, nullable=False)
    raw_html = Column(Text)  # NULLed after 90 days by `pts db prune`
    content_hash = Column(String, nullable=False)
    fetched_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    http_status = Column(Integer)
    error = Column(Text)
    # Stage 05 (0004_detail_extraction)
    fields = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    degraded = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    notes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    signals = Column(JSONB)              # F2 validation report for the winning content
    source_hash = Column(String)         # permits.raw_hash of the catalog row used (TTL probe)
    raw_responses = Column(JSONB)        # captured XHR/JSON bodies (or the SODA row); pruned with raw_html
    run_id = Column(String)              # detail_fetch_runs.run_id that wrote/refreshed it

    __table_args__ = (
        UniqueConstraint("permit_id", "content_hash", name="uq_detail_snapshots_permit_hash"),
        Index("idx_detail_snapshots_permit_fetched", "permit_id", text("fetched_at DESC")),
        CheckConstraint(_in_check("fetch_strategy", FETCH_STRATEGIES),
                        name="ck_detail_snapshots_fetch_strategy"),
    )


DETAIL_RUN_STATUSES = ("running", "completed", "failed")
DETAIL_OUTCOMES = ("ok", "failed", "skipped")


class DetailFetchRunTable(Base):
    """Run ledger for detail extraction (one row per `pts detail fetch` / ensure_fresh call)."""

    __tablename__ = "detail_fetch_runs"

    run_id = Column(String, primary_key=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False)
    trigger = Column(String, nullable=False, server_default="cli", default="cli")
    params = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    status = Column(String, nullable=False, server_default="running", default="running")
    total = Column(Integer, nullable=False, server_default=text("0"), default=0)
    ok = Column(Integer, nullable=False, server_default=text("0"), default=0)
    failed = Column(Integer, nullable=False, server_default=text("0"), default=0)
    skipped = Column(Integer, nullable=False, server_default=text("0"), default=0)
    error = Column(Text)
    started_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    completed_at = Column(DateTime)

    __table_args__ = (
        Index("idx_detail_fetch_runs_jurisdiction_started", "jurisdiction_id", text("started_at DESC")),
        CheckConstraint(_in_check("status", DETAIL_RUN_STATUSES), name="ck_detail_fetch_runs_status"),
    )


class DetailFetchResultTable(Base):
    """Per-permit outcome within a detail fetch run (`{ok, strategy, final_url, hash, error, retryable}`)."""

    __tablename__ = "detail_fetch_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("detail_fetch_runs.run_id", ondelete="CASCADE"), nullable=False)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"), nullable=False)
    permit_number = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    fetch_strategy = Column(String)
    final_url = Column(Text)
    content_hash = Column(String)
    snapshot_id = Column(BigInteger, ForeignKey("detail_snapshots.id", ondelete="SET NULL"))
    new_snapshot = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    degraded = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    error = Column(String)
    retryable = Column(Boolean)
    attempts = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    duration_ms = Column(Integer, nullable=False, server_default=text("0"), default=0)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)

    __table_args__ = (
        UniqueConstraint("run_id", "permit_id", name="uq_detail_fetch_results_run_permit"),
        CheckConstraint(_in_check("outcome", DETAIL_OUTCOMES), name="ck_detail_fetch_results_outcome"),
        CheckConstraint("fetch_strategy IS NULL OR " + _in_check("fetch_strategy", FETCH_STRATEGIES),
                        name="ck_detail_fetch_results_fetch_strategy"),
    )


class SkillInferenceTable(Base):
    """LLM trade-skill inferences per permit (written by the skill-inference stage)."""

    __tablename__ = "skill_inferences"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"), nullable=False)
    prompt_version = Column(String, nullable=False)
    model = Column(String, nullable=False)
    request_id = Column(String)
    skills = Column(JSONB, nullable=False)
    assumptions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    input_snapshot_id = Column(BigInteger, ForeignKey("detail_snapshots.id", ondelete="SET NULL"))
    review_status = Column(String, nullable=False, server_default="PENDING", default="PENDING")
    reviewer = Column(String)
    reviewed_at = Column(DateTime)
    review_note = Column(Text)
    # Stage 07 HITL (migration 0008_orchestration): who proposed the current
    # review_status ('agent' proposals are always stored as NEEDS_HUMAN) and what they proposed.
    proposed_by = Column(String)
    proposed_decision = Column(String)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    # Stage 06 audit columns (migration 0005_skill_inference_audit)
    prompt_hash = Column(String)
    completion_hash = Column(String)
    context_hash = Column(String)
    input_cache_key = Column(String)
    degraded = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    substrate_hash = Column(String)
    substrate_text = Column(Text)   # degraded only: the catalog text the quotes cite
    overall_confidence = Column(String)
    licensing_notes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    dropped_skills = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    warnings = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    signal_check = Column(JSONB)

    __table_args__ = (
        Index("idx_skill_inferences_permit_created", "permit_id", text("created_at DESC")),
        Index("idx_skill_inferences_pending", "created_at",
              postgresql_where=text("review_status = 'PENDING'")),
        Index("uq_skill_inferences_permit_cache_key", "permit_id", "input_cache_key", unique=True,
              postgresql_where=text("input_cache_key IS NOT NULL")),
        CheckConstraint(_in_check("review_status", REVIEW_STATUSES),
                        name="ck_skill_inferences_review_status"),
        CheckConstraint("overall_confidence IS NULL OR overall_confidence IN ('high', 'medium', 'low')",
                        name="ck_skill_inferences_overall_confidence"),
        CheckConstraint("proposed_by IS NULL OR proposed_by IN ('agent', 'human')",
                        name="ck_skill_inferences_proposed_by"),
        CheckConstraint("proposed_decision IS NULL OR proposed_decision IN ('approve', 'reject', 'needs_human')",
                        name="ck_skill_inferences_proposed_decision"),
    )


class EvalResultTable(Base):
    """Evaluation results: one row per scored item, per aggregate metric, and one `summary` row per run.

    Created as a stub in 0002; Stage 08 (0009_evaluation) added the suite,
    item, cache and build columns. `metric` holds the metric name as a JSON
    string ("item", "summary", "recall", ...). `cache_key` =
    sha256(suite, pinned input hash, model, prompt version): a re-run with the
    same key reuses `payload` instead of calling the LLM.
    """

    __tablename__ = "eval_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"))
    metric = Column(JSONB, nullable=False)
    value = Column(Numeric)
    threshold = Column(Numeric)
    passed = Column(Boolean)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    # Stage 08 (0009_evaluation)
    suite = Column(String)
    build_id = Column(String)
    item_key = Column(String)
    input_hash = Column(String)
    model = Column(String)
    prompt_version = Column(String)
    cache_key = Column(String)
    payload = Column(JSONB)

    __table_args__ = (
        Index("idx_eval_results_run", "run_id"),
        Index("idx_eval_results_suite_created", "suite", text("created_at DESC")),
        Index("idx_eval_results_build_suite", "build_id", "suite"),
        Index("idx_eval_results_cache_key", "cache_key", postgresql_where=text("cache_key IS NOT NULL")),
    )


class NluCacheTable(Base):
    """Cached natural-language parses (Stage 04 `POST /api/v1/query`, 24h TTL).

    key = sha256(question, jurisdiction, NLU schema version, today's date).
    Expired rows are ignored on read and deleted by `pts db prune`.
    """

    __tablename__ = "nlu_cache"

    key = Column(String, primary_key=True)
    question = Column(Text, nullable=False)
    jurisdiction = Column(String)
    schema_version = Column(String, nullable=False)
    model = Column(String, nullable=False)
    response = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_nlu_cache_expires", "expires_at"),
    )


# ---- Stage 07 orchestration (migration 0008_orchestration) ---------------------

PIPELINE_RUN_STATUSES = ("running", "completed", "stopped", "failed")
BACKLOG_STAGES = ("detail", "skill_infer")


class OrchestrationEventTable(Base):
    """Append-only trail of every planner tool call, pipeline step, state transition and review decision.

    One `run_id` per pipeline run or agent goal. `params_json` / `result_json`
    let `pts agent replay` re-run a transcript against current data.
    """

    __tablename__ = "orchestration_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="SET NULL"))
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    event = Column(String, nullable=False)
    from_state = Column(String)
    to_state = Column(String)
    tool = Column(String)
    params_json = Column(JSONB)
    result_json = Column(JSONB)
    ok = Column(Boolean, nullable=False, server_default=text("true"), default=True)
    error_code = Column(String)
    retryable = Column(Boolean)
    latency_ms = Column(Integer)

    __table_args__ = (
        Index("idx_orchestration_events_run", "run_id", "id"),
        Index("idx_orchestration_events_permit_created", "permit_id", "created_at"),
        Index("brin_orchestration_events_created_at", "created_at", postgresql_using="brin"),
    )


class PipelineRunTable(Base):
    """One row per `pts pipeline run` (the run report)."""

    __tablename__ = "pipeline_runs"

    run_id = Column(String, primary_key=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False)
    trigger = Column(String, nullable=False, server_default="cli", default="cli")
    params = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    status = Column(String, nullable=False, server_default="running", default="running")
    planned = Column(Integer, nullable=False, server_default=text("0"), default=0)
    completed = Column(Integer, nullable=False, server_default=text("0"), default=0)
    failed = Column(Integer, nullable=False, server_default=text("0"), default=0)
    skipped = Column(Integer, nullable=False, server_default=text("0"), default=0)
    queued = Column(Integer, nullable=False, server_default=text("0"), default=0)
    llm_calls = Column(Integer, nullable=False, server_default=text("0"), default=0)
    tokens = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    est_cost = Column(Numeric)
    started_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    finished_at = Column(DateTime)
    duration_ms = Column(Integer)
    error = Column(Text)

    __table_args__ = (
        Index("idx_pipeline_runs_jurisdiction_started", "jurisdiction_id", text("started_at DESC")),
        CheckConstraint(_in_check("status", PIPELINE_RUN_STATUSES), name="ck_pipeline_runs_status"),
    )


class RetryBacklogTable(Base):
    """Cross-run retry state of a permit's failed step (with permits.processing_state, the durable queue)."""

    __tablename__ = "retry_backlog"

    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="CASCADE"), primary_key=True)
    stage = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, server_default=text("0"), default=0)
    next_retry_at = Column(DateTime)
    exhausted = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    last_error = Column(JSONB)
    last_run_id = Column(String)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    updated_at = Column(DateTime, nullable=False, server_default=UTC_NOW)

    __table_args__ = (
        Index("idx_retry_backlog_next_retry", "next_retry_at", postgresql_where=text("NOT exhausted")),
        CheckConstraint(_in_check("stage", BACKLOG_STAGES), name="ck_retry_backlog_stage"),
    )


# ---- Stage 08 evaluation (migration 0009_evaluation) ---------------------------

LABEL_KINDS = ("trade_set", "nl_query", "extraction_checklist")
LABEL_ROLES = ("labeler", "adjudicator")
GATE_STATUSES = ("PASS", "CONDITIONAL", "FAIL")
ALERT_SEVERITIES = ("info", "warning", "critical")


class LabelTable(Base):
    """Human (or draft) labels for the golden sets. Never overwritten: a changed input supersedes a row.

    `pinned_input_hash` binds the label to the exact input it was made
    against; `superseded_by` points at the row that replaced it.
    """

    __tablename__ = "labels"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id", ondelete="CASCADE"), nullable=False)
    permit_id = Column(BigInteger, ForeignKey("permits.id", ondelete="SET NULL"))
    kind = Column(String, nullable=False)
    item_key = Column(String, nullable=False)         # permit number, or the NL question id
    role = Column(String, nullable=False, server_default="labeler", default="labeler")
    payload = Column(JSONB, nullable=False)
    labeled_by = Column(String, nullable=False)
    label_version = Column(Integer, nullable=False, server_default=text("1"), default=1)
    pinned_input_hash = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)
    superseded_by = Column(BigInteger, ForeignKey("labels.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("idx_labels_kind_item", "kind", "item_key", "created_at"),
        Index("uq_labels_current", "jurisdiction_id", "kind", "item_key", "role", "labeled_by", unique=True,
              postgresql_where=text("superseded_by IS NULL")),
        CheckConstraint(_in_check("kind", LABEL_KINDS), name="ck_labels_kind"),
        CheckConstraint(_in_check("role", LABEL_ROLES), name="ck_labels_role"),
    )


class BuildGateTable(Base):
    """One `pts quality gate` report per evaluation (re-runnable; the newest row per build counts)."""

    __tablename__ = "build_gates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    build_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    report = Column(JSONB, nullable=False)
    signed_off_by = Column(String)
    signed_off_at = Column(DateTime)
    sign_off_note = Column(Text)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)

    __table_args__ = (
        Index("idx_build_gates_build_created", "build_id", text("created_at DESC")),
        CheckConstraint(_in_check("status", GATE_STATUSES), name="ck_build_gates_status"),
    )


class DriftAlertTable(Base):
    """A drift detector breach (`pts alerts check`), with the suspected cause for on-call triage."""

    __tablename__ = "drift_alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    jurisdiction_id = Column(String, ForeignKey("jurisdictions.id", ondelete="CASCADE"))
    detector = Column(String, nullable=False)
    severity = Column(String, nullable=False, server_default="warning", default="warning")
    suspected_cause = Column(String)
    message = Column(Text, nullable=False)
    measured = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    baseline = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    window_start = Column(DateTime)
    window_end = Column(DateTime)
    notified = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    acknowledged_by = Column(String)
    acknowledged_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, server_default=UTC_NOW)

    __table_args__ = (
        Index("idx_drift_alerts_created", text("created_at DESC")),
        CheckConstraint(_in_check("severity", ALERT_SEVERITIES), name="ck_drift_alerts_severity"),
    )


class Registry:
    """Database registry for managing jurisdictions."""

    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def create_tables(self) -> None:
        """Bring the schema up to date (idempotent).

        Runs the Alembic migrations to head. A database created before Alembic
        (tables present, no alembic_version) is stamped at the 0001_initial
        baseline first. Refuses revisions marked --destructive; use
        `pts db migrate --destructive` for those.
        """
        from permit_trade_skills.store.schema import ensure_schema

        ensure_schema(self.engine)

    def get_session(self):
        """Get a database session."""
        return self.SessionLocal()

    def jurisdiction_exists(self, session, jurisdiction_id: str) -> bool:
        """Check if a jurisdiction exists."""
        return session.query(exists().where(JurisdictionTable.id == jurisdiction_id)).scalar()

    # Config columns kept in jurisdiction_versions.config (api_key is not copied).
    CONFIG_COLUMNS = ("name", "state", "county", "platform_type", "base_url", "api_endpoint",
                      "dataset_id", "dataset_name", "field_map", "status_map", "detail_strategy",
                      "is_active")

    @classmethod
    def config_snapshot(cls, row: "JurisdictionTable") -> Dict[str, Any]:
        """The config columns of a jurisdiction row as a JSON-ready dict."""
        snapshot = {c: getattr(row, c) for c in cls.CONFIG_COLUMNS}
        snapshot["api_key_set"] = bool(row.api_key)
        snapshot["created_at"] = row.created_at.isoformat() if row.created_at else None
        snapshot["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
        return snapshot

    def add_jurisdiction(self, session, jurisdiction: Jurisdiction,
                         replaced_by: Optional[str] = None) -> JurisdictionTable:
        """Insert a jurisdiction, or update the existing row with the same id.

        Re-registering an existing id updates that row in place (so sync, which
        looks jurisdictions up by exact id, sees the new config) and bumps
        config_version so sync reports record which config produced them.
        created_at is preserved. The replaced config is first copied to
        jurisdiction_versions (with `replaced_by`, e.g. the command), so any
        past config_version can be recovered.
        """
        now = datetime.utcnow()
        values = dict(
            name=jurisdiction.name,
            state=jurisdiction.state,
            county=jurisdiction.county,
            platform_type=jurisdiction.platform_type.value,
            base_url=jurisdiction.base_url,
            api_endpoint=jurisdiction.api_endpoint,
            api_key=jurisdiction.api_key,
            dataset_id=jurisdiction.dataset_id,
            dataset_name=jurisdiction.dataset_name,
            field_map=jurisdiction.field_map,
            status_map=jurisdiction.status_map,
            detail_strategy=jurisdiction.detail_strategy,
            is_active=jurisdiction.is_active,
        )

        db_jurisdiction = session.get(JurisdictionTable, jurisdiction.id)
        if db_jurisdiction is not None:
            session.add(JurisdictionVersionTable(
                jurisdiction_id=db_jurisdiction.id,
                config_version=db_jurisdiction.config_version,
                config=self.config_snapshot(db_jurisdiction),
                replaced_by=replaced_by,
                created_at=now,
            ))
            for key, value in values.items():
                setattr(db_jurisdiction, key, value)
            db_jurisdiction.config_version = (db_jurisdiction.config_version or 0) + 1
            db_jurisdiction.updated_at = now
        else:
            db_jurisdiction = JurisdictionTable(
                id=jurisdiction.id,
                config_version=jurisdiction.config_version,
                created_at=jurisdiction.created_at or now,
                updated_at=jurisdiction.updated_at or now,
                **values,
            )
            session.add(db_jurisdiction)

        session.commit()
        session.refresh(db_jurisdiction)

        return db_jurisdiction

    def get_jurisdiction(self, session, jurisdiction_id: str) -> Optional[JurisdictionTable]:
        """Get a jurisdiction by ID."""
        return session.query(JurisdictionTable).filter_by(id=jurisdiction_id).first()

    def list_active_jurisdictions(self, session) -> List[JurisdictionTable]:
        """List all active jurisdictions."""
        return session.query(JurisdictionTable).filter_by(is_active=True).all()

    def resolve_jurisdiction(self, session, id_or_prefix: str) -> JurisdictionTable:
        """The jurisdiction with this exact id, else the one active jurisdiction
        whose id starts with it. Callers must use the returned row's `id`.

        Raises LookupError when nothing matches or the prefix is ambiguous.
        """
        exact = self.get_jurisdiction(session, id_or_prefix)
        if exact is not None:
            return exact
        matches = sorted(
            (j for j in self.list_active_jurisdictions(session) if j.id.startswith(id_or_prefix)),
            key=lambda j: j.id,
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"Jurisdiction '{id_or_prefix}' not found in registry")
        raise LookupError(
            f"Jurisdiction '{id_or_prefix}' is ambiguous: "
            + ", ".join(j.id for j in matches) + ". Use the full id."
        )

    def get(self, id_or_prefix: str) -> JurisdictionConfig:
        """Typed config of a jurisdiction (exact id or unique active prefix).

        Raises LookupError when nothing matches or the prefix is ambiguous.
        """
        import json

        session = self.get_session()
        try:
            row = self.resolve_jurisdiction(session, id_or_prefix)
            values = {c: getattr(row, c) for c in JurisdictionConfig.model_fields if hasattr(row, c)}
        finally:
            session.close()
        for key in ("field_map", "status_map", "detail_strategy"):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
            values[key] = values.get(key) or {}
        return JurisdictionConfig(**values)

    def list_versions(self, session, jurisdiction_id: str) -> List["JurisdictionVersionTable"]:
        """Replaced configs of a jurisdiction, oldest first."""
        return (session.query(JurisdictionVersionTable)
                .filter_by(jurisdiction_id=jurisdiction_id)
                .order_by(JurisdictionVersionTable.config_version).all())

    def save_discovery_run(self, session, run: DiscoveryRun) -> str:
        """Insert a discovery run (jurisdiction_id may be NULL until registration); returns its id."""
        import uuid

        run_id = run.id or str(uuid.uuid4())
        session.add(DiscoveryRunTable(
            id=run_id,
            jurisdiction_id=run.jurisdiction_id,
            search_query=run.search_query,
            search_parameters=run.search_parameters,
            total_results=run.total_results,
            candidates_found=run.candidates_found,
            status=run.status.value if hasattr(run.status, "value") else run.status,
            error_message=run.error_message,
            started_at=run.started_at,
            completed_at=run.completed_at,
        ))
        session.commit()
        return run_id

    def link_discovery_run(self, session, run_id: str, jurisdiction_id: str) -> bool:
        """Point a discovery run at the jurisdiction registered from it. False if the run is unknown."""
        run = session.get(DiscoveryRunTable, run_id)
        if run is None:
            return False
        run.jurisdiction_id = jurisdiction_id
        session.commit()
        return True

    def save_dossier(self, session, dossier: DiscoveryDossier) -> None:
        """Save a discovery dossier to the database."""
        # Probes need a jurisdiction row (FK). Only create a placeholder when
        # none exists, so the real config saved by register is never clobbered.
        db_jurisdiction = self.get_jurisdiction(session, dossier.jurisdiction_id)
        if db_jurisdiction is None:
            db_jurisdiction = self._add_placeholder_jurisdiction(session, dossier)

        # Save probe results (merge = upsert; probe ids are deterministic)
        for result in dossier.verification_results:
            db_probe = ProbeTable(
                id=f"{dossier.jurisdiction_id}_probe_{result.probe_name}",
                jurisdiction_id=db_jurisdiction.id,
                probe_name=result.probe_name,
                success=result.success,
                detected_platform=result.detected_platform.value if result.detected_platform else None,
                confidence=int(result.confidence * 100) if result.confidence else None,
                details=result.details,
                error_message=result.error_message,
                executed_at=datetime.utcnow(),
            )
            session.merge(db_probe)

        session.commit()

    def _add_placeholder_jurisdiction(self, session, dossier: DiscoveryDossier) -> JurisdictionTable:
        """Register a minimal jurisdiction from a dossier (before verification)."""
        jurisdiction = Jurisdiction(
            id=dossier.jurisdiction_id,
            name=dossier.jurisdiction_name,
            state=dossier.jurisdiction_name.split()[0] if dossier.jurisdiction_name else "",
            county=None,
            platform_type=PlatformType.OTHER,  # Will be updated after verification
            base_url="",
            config_version=1,
            field_map=dossier.field_mapping,
            status_map=dossier.status_mapping,
            detail_strategy=dossier.detail_strategy,
            is_active=True,
        )
        return self.add_jurisdiction(session, jurisdiction)

    def export_config(self, session, jurisdiction_id: str) -> Optional[Dict[str, Any]]:
        """Export jurisdiction configuration to dict."""
        jurisdiction = self.get_jurisdiction(session, jurisdiction_id)
        if not jurisdiction:
            return None

        return {
            "id": jurisdiction.id,
            "name": jurisdiction.name,
            "state": jurisdiction.state,
            "county": jurisdiction.county,
            "platform_type": jurisdiction.platform_type,
            "base_url": jurisdiction.base_url,
            "api_endpoint": jurisdiction.api_endpoint,
            "api_key": jurisdiction.api_key,
            "dataset_id": jurisdiction.dataset_id,
            "dataset_name": jurisdiction.dataset_name,
            "config_version": jurisdiction.config_version,
            "field_map": jurisdiction.field_map,
            "status_map": jurisdiction.status_map,
            "detail_strategy": jurisdiction.detail_strategy,
            "is_active": jurisdiction.is_active,
            "created_at": jurisdiction.created_at.isoformat() if jurisdiction.created_at else None,
            "updated_at": jurisdiction.updated_at.isoformat() if jurisdiction.updated_at else None,
        }

    def save_candidates(
        self,
        session,
        jurisdiction_id: str,
        candidates: List[DiscoveryCandidate],
    ) -> None:
        """Save discovery candidates (in a future extension)."""
        # This is a placeholder for future implementation
        # Candidates are currently stored in memory/dossier
        pass

    def get_jurisdiction(self, session, jurisdiction_id: str):
        """Get a jurisdiction by ID."""
        return session.query(JurisdictionTable).filter_by(id=jurisdiction_id).first()

    def list_active_jurisdictions(self, session):
        """List all active jurisdictions."""
        return session.query(JurisdictionTable).filter_by(is_active=True).all()
    
    def get_permit_by_number(self, session, jurisdiction_id: str, permit_number: str):
        """Get a permit by jurisdiction_id and permit_number."""
        return session.query(PermitTable).filter_by(
            jurisdiction_id=jurisdiction_id,
            permit_number=permit_number
        ).first()
    
    def save_sync_report(self, session, report):
        """Save sync report to database."""
        from permit_trade_skills.ingest.schemas import SyncReport
        sync_report = SyncMetadataTable(
            jurisdiction_id=report.jurisdiction_id,
            jurisdiction_slug=report.jurisdiction_slug,
            run_id=report.run_id,
            config_version=report.config_version,
            pull_count=report.pull_count,
            inserted=report.inserted,
            updated=report.updated,
            unchanged=report.unchanged,
            dropped=report.dropped,
            warnings=report.warnings,
            duration_ms=report.duration_ms,
            cursor_before=report.cursor_before,
            cursor_after=report.cursor_after,
            error=report.error,
            started_at=datetime.utcnow() - timedelta(milliseconds=report.duration_ms) if report.duration_ms else datetime.utcnow(),
            completed_at=report.completed_at,
            mode=report.mode,
            status_transitions=report.status_transitions,
            max_applied_at=report.max_applied_at,
            max_synced_at=report.max_synced_at,
            fingerprint=report.fingerprint,
        )
        session.add(sync_report)
    
    def get_latest_sync_report(self, session, jurisdiction_id: str):
        """Get the latest sync report for a jurisdiction (by jurisdiction id)."""
        report = session.query(SyncMetadataTable).filter_by(
            jurisdiction_id=jurisdiction_id
        ).order_by(SyncMetadataTable.completed_at.desc()).first()
        
        if report:
            return {
                'run_id': report.run_id,
                'cursor_after': report.cursor_after,
                'completed_at': report.completed_at,
                'fingerprint': report.fingerprint,
                'error': report.error,
            }
        return None

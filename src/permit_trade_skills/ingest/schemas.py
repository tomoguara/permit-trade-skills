"""Data models and schemas for the ingestion module."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Status(Enum):
    """Canonical permit status values."""
    PENDING = "PENDING"
    ISSUED = "ISSUED"
    IN_INSPECTION = "IN_INSPECTION"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class ProcessingState(Enum):
    """Permit processing lifecycle states (permits.processing_state).

    Advanced with optimistic compare-and-set via
    PermitRepository.advance_state(permit_id, from, to).
    """
    SYNCED = "SYNCED"
    DETAIL_READY = "DETAIL_READY"
    SKILLS_INFERRED = "SKILLS_INFERRED"
    SKILLS_APPROVED = "SKILLS_APPROVED"
    SKILLS_REJECTED = "SKILLS_REJECTED"
    DETAIL_FAILED = "DETAIL_FAILED"
    SKILLS_FAILED = "SKILLS_FAILED"
    SKILLS_SKIPPED = "SKILLS_SKIPPED"


class SyncMode(Enum):
    """How a sync run was executed (sync_metadata.mode)."""
    FULL = "full"
    INCREMENTAL = "incremental"
    DRY_RUN = "dry_run"


class PermitRecord(BaseModel):
    """Canonical permit record: what `PermitNormalizer.normalize_record` builds from a raw row.

    `to_row()` gives the dict the sync upsert writes to `permits`.
    `permit_number` may be None here: such rows are dropped (and counted) by sync.
    """

    model_config = ConfigDict(use_enum_values=True)

    jurisdiction_id: str
    jurisdiction_slug: str
    permit_number: Optional[str] = None
    permit_class: Optional[str] = None
    permit_class_mapped: Optional[str] = None
    permit_type_mapped: Optional[str] = None
    permit_type_desc: Optional[str] = None
    status_raw: Optional[str] = None
    status: Status = Status.UNKNOWN
    status_map_key: Optional[str] = None
    issue_date: Optional[datetime] = None
    issued_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    description: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None  # address state (the lifecycle is processing_state)
    zip_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    valuation: Optional[float] = None
    work_class: Optional[str] = None
    building_count: Optional[int] = None
    housing_units: Optional[int] = None
    housing_units_added: Optional[int] = None
    housing_units_removed: Optional[int] = None
    housing_category: Optional[str] = None
    contractor_name: Optional[str] = None
    applicant_name: Optional[str] = None
    raw_row: Dict[str, Any]
    raw_hash: str
    row_id_source: Optional[str] = None
    row_version_source: Optional[str] = None
    warnings: List[Dict[str, Any]] = Field(default_factory=list)
    source_updated_at: Optional[datetime] = None
    source_synced_at: Optional[datetime] = None
    detail_url: Optional[str] = None
    detail_strategy: Optional[str] = None
    detail_fetch_type: Optional[str] = None
    run_id: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    synced_at: Optional[datetime] = None
    processing_state: ProcessingState = ProcessingState.SYNCED

    def to_row(self) -> Dict[str, Any]:
        """Plain dict for the upsert (enums as their string values; raw_row as given)."""
        row = self.model_dump()
        row["raw_row"] = self.raw_row  # unmodified (pydantic keeps the source key order)
        return row


class SyncReport(BaseModel):
    """Sync run report."""
    
    jurisdiction_id: str
    jurisdiction_slug: str
    run_id: str
    config_version: int
    pull_count: int
    inserted: int
    updated: int
    unchanged: int
    dropped: int
    warnings: List[Dict[str, Any]]
    duration_ms: int
    cursor_before: Optional[str] = None
    cursor_after: Optional[str] = None
    error: Optional[str] = None
    completed_at: datetime
    mode: str = SyncMode.FULL.value
    status_transitions: Dict[str, int] = {}  # "FROM->TO" -> count
    max_applied_at: Optional[datetime] = None   # latest applied date pulled
    max_synced_at: Optional[datetime] = None    # latest source :updated_at pulled
    fingerprint: Optional[str] = None           # pulled-set fingerprint (see sync)

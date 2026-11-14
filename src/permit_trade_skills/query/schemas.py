"""Response/request models for the Stage 04 query API (frozen contract, see docs/api.md).

Field names follow the canonical store (`PermitQuery` and the `permits`
columns): `valuation`, `zip_code`, `processing_state`, `desc_keyword`, ...
Timestamps are ISO-8601 UTC strings with a trailing `Z`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Problem(_Model):
    """RFC 9457-style problem body (`application/problem+json`)."""

    type: str = "about:blank"
    title: str
    status: int
    error_code: str
    message: str
    trace_id: str
    retryable: bool = False
    registered_jurisdictions: Optional[List[str]] = None
    candidates: Optional[List[str]] = None


class Jurisdiction(_Model):
    slug: str
    name: str
    county: Optional[str]
    state: str
    platform: str
    dataset_id: Optional[str]
    record_count: int
    last_synced_at: Optional[str]


class JurisdictionsResponse(_Model):
    jurisdictions: List[Jurisdiction]
    count: int
    trace_id: Optional[str] = None


class PermitRow(_Model):
    permit_number: str
    status_raw: Optional[str]
    status: str
    permit_class: Optional[str]
    permit_class_mapped: Optional[str]
    permit_type_mapped: Optional[str]
    permit_type_desc: Optional[str]
    description: Optional[str]
    valuation: Optional[float]
    applied_at: Optional[str]
    issued_at: Optional[str]
    expires_at: Optional[str]
    completed_at: Optional[str]
    address: Optional[str]
    city: Optional[str]
    zip_code: Optional[str]
    detail_url: Optional[str]
    processing_state: str
    trade_confidence: Optional[Dict[str, str]] = Field(
        default=None, description="Trade queries only: the best confidence of each requested trade "
                                  "in the permit's latest skill inference (trades it lacks are omitted)")


class TradeCoverage(_Model):
    """How much of the result set the `trade` filter could see (trade filters only)."""

    candidates: int = Field(description="Permits matching every filter except trade (exact)")
    inferred: int = Field(description="Of those, permits with a usable (non-REJECTED) skill inference")
    not_inferred: int = Field(description="candidates - inferred: never matched by trade, whatever their work")
    note: Optional[str] = Field(default=None, description="Warning shown when not_inferred > 0")


class PermitsResponse(_Model):
    jurisdiction: str
    query: Dict[str, Any] = Field(description="The exact PermitQuery executed (set fields only)")
    total_estimate: int = Field(description="Planner estimate of all matching rows (not exact); "
                                            "exact when trade_coverage is set")
    count: int
    next_cursor: Optional[str] = Field(description="Opaque keyset token for the next page; null on the last page")
    rows: List[PermitRow]
    trade_coverage: Optional[TradeCoverage] = Field(
        default=None, description="Set when the query filters by trade: inference coverage of the candidates")
    trace_id: Optional[str] = None


class StatusChange(_Model):
    from_status: Optional[str]
    to_status: str
    raw_status: Optional[str]
    changed_at: Optional[str]
    changed_by: Optional[str]
    run_id: Optional[str]


class DetailSnapshotInfo(_Model):
    present: bool
    fetched_at: Optional[str] = None
    final_url: Optional[str] = None
    fetch_strategy: Optional[str] = None


class SkillInferenceInfo(_Model):
    id: int
    model: str
    prompt_version: str
    review_status: str
    created_at: Optional[str]
    skills: List[Dict[str, Any]]
    assumptions: List[Any]


class PermitDetail(PermitRow):
    state: Optional[str] = Field(description="Address state (e.g. WA)")
    latitude: Optional[float]
    longitude: Optional[float]
    housing_units: Optional[int]
    housing_units_added: Optional[int]
    housing_units_removed: Optional[int]
    housing_category: Optional[str]
    work_class: Optional[str]
    contractor_name: Optional[str]
    warnings: List[Any]
    state_updated_at: Optional[str]
    state_error: Optional[Any]
    source_updated_at: Optional[str]
    synced_at: Optional[str]


class PermitDetailResponse(_Model):
    jurisdiction: str
    permit: PermitDetail
    status_history: List[StatusChange]
    detail_snapshot: DetailSnapshotInfo
    skill_inference: Optional[SkillInferenceInfo]
    trace_id: Optional[str] = None


class StatusCountsResponse(_Model):
    jurisdiction: str
    since: Optional[str]
    until: Optional[str]
    date_field: str
    filters: Dict[str, Any]
    counts: Dict[str, int] = Field(description="Every canonical status (0 when none); UNKNOWN only when > 0")
    total: int
    trace_id: Optional[str] = None


class QueryRequest(_Model):
    question: str = Field(min_length=1, max_length=1000)
    jurisdiction: Optional[str] = None
    page_size: Optional[int] = Field(default=None, ge=1)
    cursor: Optional[str] = None


class Clarification(_Model):
    text: str
    llm_generated: bool = True


class NluInfo(_Model):
    model: str
    prompt_version: str
    schema_version: str
    cached: bool
    request_id: Optional[str] = None


class QueryResponse(PermitsResponse):
    question: str
    echo: str = Field(description="Template-rendered from `query` (never LLM-written)")
    interpreted: Dict[str, Any] = Field(description="The NLU's permit_query, before validation")
    unfulfilled_filters: List[str]
    assumptions: List[str]
    ambiguous_terms: List[str]
    executed: bool = Field(description="False when the core requirement could not be expressed as a query")
    clarification: Optional[Clarification]
    nlu: NluInfo

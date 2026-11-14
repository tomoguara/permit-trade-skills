"""Data models and schemas for the discovery module."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PlatformType(str, Enum):
    """Supported data platforms."""

    SOCRATA = "socrata"
    CKAN = "ckan"
    OTHER = "other"


class DiscoveryStatus(str, Enum):
    """Status of a discovery run."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationResult(str, Enum):
    """Result of platform verification."""

    DETECTED = "detected"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class Jurisdiction(BaseModel):
    """Represents a jurisdiction (city, county, state) with its data configuration."""

    id: Optional[str] = None
    name: str
    state: str
    county: Optional[str] = None
    platform_type: PlatformType
    base_url: str
    api_endpoint: Optional[str] = None
    api_key: Optional[str] = None
    dataset_id: Optional[str] = None
    dataset_name: Optional[str] = None
    config_version: int = 1
    field_map: Dict[str, Any] = Field(default_factory=dict)
    status_map: Dict[str, Any] = Field(default_factory=dict)
    detail_strategy: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DiscoveryCandidate(BaseModel):
    """A dataset candidate from catalog discovery."""

    id: str
    title: str
    organization: str
    url: str
    # None until a probe detects the platform (catalog search results start unknown).
    platform_type: Optional[PlatformType] = None
    platform_detected_by: str
    confidence: float = Field(ge=0, le=1)
    # Per-factor scores behind `confidence` (factor -> {"score", "weight"}), set by Scorer.
    score_breakdown: Dict[str, Any] = Field(default_factory=dict)
    # Catalog (DCAT) fields, when the catalog provides them.
    identifier: Optional[str] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    distributions: List[Dict[str, Any]] = Field(default_factory=list)  # format, downloadURL, describedBy
    metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    is_match: bool = False
    verified_at: Optional[datetime] = None


class DiscoveryRun(BaseModel):
    """A single discovery run record."""

    id: Optional[str] = None
    search_query: str
    search_parameters: Dict[str, Any]
    jurisdiction_id: Optional[str] = None
    total_results: int = 0
    candidates_found: int = 0
    status: DiscoveryStatus
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ProbeResult(BaseModel):
    """Result of a platform verification probe."""

    probe_name: str
    success: bool
    detected_platform: Optional[PlatformType] = None
    confidence: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


class DiscoveryDossier(BaseModel):
    """Complete evidence dossier for a discovered jurisdiction."""

    jurisdiction_id: str
    jurisdiction_name: str
    candidates: List[DiscoveryCandidate]
    verification_results: List[ProbeResult]
    selected_candidate: Optional[DiscoveryCandidate] = None
    field_mapping: Dict[str, Any] = Field(default_factory=dict)
    status_mapping: Dict[str, Any] = Field(default_factory=dict)
    detail_strategy: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    discovery_run_id: Optional[str] = None  # discovery_runs.id written by `discover`
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JurisdictionConfig(BaseModel):
    """A registered jurisdiction's config, as read by ingestion (`Registry.get`).

    The typed view of a `jurisdictions` row: the jsonb columns are parsed dicts.
    """

    id: str
    name: str
    state: str
    county: Optional[str] = None
    platform_type: str
    base_url: Optional[str] = None
    api_endpoint: Optional[str] = None
    dataset_id: Optional[str] = None
    dataset_name: Optional[str] = None
    config_version: int
    field_map: Dict[str, str] = Field(default_factory=dict)
    status_map: Dict[str, List[str]] = Field(default_factory=dict)
    detail_strategy: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True

    @property
    def slug(self) -> str:
        return self.id

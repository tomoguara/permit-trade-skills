"""Data types shared by the detail-extraction stage (no I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Stored `detail_snapshots.fetch_strategy` values (CHECK constraint since 0002).
SODA_ROW = "soda_row"
REDIRECT_CHASE = "redirect_chase"
PLAYWRIGHT = "playwright"
STRATEGIES = (SODA_ROW, REDIRECT_CHASE, PLAYWRIGHT)
PORTAL_STRATEGIES = (REDIRECT_CHASE, PLAYWRIGHT)

# Error codes recorded in FetchResult.error / detail_fetch_results.error.
REDIRECT_BLOCKED = "REDIRECT_BLOCKED"        # redirect (or start URL) to a host off the allow-list
HUMAN_GATE = "HUMAN_GATE"                    # CAPTCHA / bot challenge detected
HUMAN_GATE_ACTIVE = "HUMAN_GATE_ACTIVE"      # host is cooling down after a HUMAN_GATE
ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
HTTP_ERROR = "HTTP_ERROR"                    # non-retryable 4xx (403, 404, ...)
HTTP_RETRYABLE = "HTTP_RETRYABLE"            # 429 / 5xx after the retry budget
TIMEOUT = "TIMEOUT"
NETWORK = "NETWORK"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
NOT_FOUND = "NOT_FOUND"                      # SODA query returned no row
NO_DETAIL_URL = "NO_DETAIL_URL"
VALIDATION_FAILED = "VALIDATION_FAILED"      # fetched, but < min_signals (F2)
BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"  # playwright package or chromium missing
UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"


@dataclass(frozen=True)
class CapturedResponse:
    """A network response body captured during a fetch (XHR/JSON, or the SODA row)."""

    url: str
    type: str          # "json" | "html" | "text"
    body: str


@dataclass
class FetchResult:
    ok: bool
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    html: Optional[str] = None
    responses: List[CapturedResponse] = field(default_factory=list)
    error: Optional[str] = None
    retryable: bool = False
    detail: Optional[str] = None        # human-readable error detail
    retry_after_s: Optional[float] = None
    requested_urls: List[str] = field(default_factory=list)  # network log (every URL requested)
    blocked_urls: List[str] = field(default_factory=list)    # URLs refused by the allow-list
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PermitRef:
    """The catalog view of one permit, as the detail stage needs it."""

    permit_id: int
    jurisdiction: str
    permit_number: str
    detail_url: Optional[str]
    raw_row: Dict[str, Any]
    raw_hash: Optional[str]
    status: Optional[str] = None
    processing_state: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    issued_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    valuation: Optional[float] = None

    def dates(self) -> List[datetime]:
        return [d for d in (self.issued_at, self.applied_at, self.completed_at, self.expires_at) if d]


@dataclass
class SignalReport:
    """F2 relevance/completeness validation of candidate content."""

    count: int
    min_signals: int
    anchor: bool
    matched: Dict[str, bool]
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.anchor and self.count >= self.min_signals

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "min_signals": self.min_signals, "anchor": self.anchor,
                "passed": self.passed, "matched": dict(self.matched), "details": dict(self.details)}


# The DetailDocument contract consumed by Stage 06 (skills) and Stage 07
# (orchestration). tests/integration/test_detail_contract.py pins these keys.
DETAIL_DOCUMENT_KEYS = (
    "permit_number", "jurisdiction", "snapshot_id", "final_url", "content_text", "fields",
    "fetch_strategy", "fetched_at", "content_hash", "degraded", "notes", "signals",
)


@dataclass
class DetailDocument:
    permit_number: str
    jurisdiction: str
    final_url: str
    content_text: str
    fields: Dict[str, Any]
    fetch_strategy: str
    content_hash: str
    degraded: bool = False
    notes: List[str] = field(default_factory=list)
    signals: Optional[Dict[str, Any]] = None
    fetched_at: Optional[datetime] = None
    snapshot_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.isoformat() + "Z" if self.fetched_at else None
        return {k: d[k] for k in DETAIL_DOCUMENT_KEYS}


@dataclass
class Attempt:
    strategy: str
    ok: bool
    url: Optional[str] = None
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    error: Optional[str] = None
    retryable: Optional[bool] = None
    tries: int = 0
    signals: Optional[int] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class PermitOutcome:
    """Per-permit result of a pipeline run (one detail_fetch_results row)."""

    permit_id: int
    permit_number: str
    outcome: str                       # ok | failed | skipped
    strategy: Optional[str] = None
    final_url: Optional[str] = None
    content_hash: Optional[str] = None
    snapshot_id: Optional[int] = None
    new_snapshot: bool = False
    degraded: bool = False
    error: Optional[str] = None
    retryable: Optional[bool] = None
    attempts: List[Attempt] = field(default_factory=list)
    duration_ms: int = 0
    document: Optional[DetailDocument] = None
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permit_number": self.permit_number, "ok": self.ok, "outcome": self.outcome,
            "strategy": self.strategy, "final_url": self.final_url, "hash": self.content_hash,
            "snapshot_id": self.snapshot_id, "new_snapshot": self.new_snapshot,
            "degraded": self.degraded, "error": self.error, "retryable": self.retryable,
            "attempts": [a.to_dict() for a in self.attempts], "duration_ms": self.duration_ms,
            "notes": list(self.notes),
        }


FieldOrder = List[Tuple[str, str]]   # [(LABEL, source column), ...]

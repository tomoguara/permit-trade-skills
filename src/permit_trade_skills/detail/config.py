"""Per-jurisdiction detail configuration (registry `jurisdictions.detail_strategy.detail`).

Everything jurisdiction-specific lives in the registry; this module only
parses it and fills defaults. A jurisdiction with no `detail` block gets
`fetch_strategy=soda_row` (the SODA row is always available for a Socrata
dataset) and, for portal strategies, `allowed_hosts` = the detail URL's host.

Registry shape (all keys optional):

    detail_strategy:
      detail:
        fetch_strategy: redirect_chase_then_soda   # see PLANS
        allowed_hosts: [services.seattle.gov]
        soda_resource_url: https://.../resource/<dataset>.json
        soda_key_column: permitnum
        min_signals: 4
        rate_limits_per_minute: {services.seattle.gov: 5, cos-data.seattle.gov: 20}
        field_order: [[PERMIT, permitnum], [STATUS, statuscurrent], ...]
        portal:
          include: ["#record-summary", ".record-body"]   # DOM regions to extract (default: <body>)
          field_labels: {"Record Number": permit_number, ...}  # portal label -> fields{} key
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from permit_trade_skills.detail.models import PLAYWRIGHT, REDIRECT_CHASE, SODA_ROW

# fetch_strategy (plan) -> ordered strategies to try.
PLANS: Dict[str, Tuple[str, ...]] = {
    "soda_row": (SODA_ROW,),
    "redirect_chase": (REDIRECT_CHASE,),
    "playwright": (PLAYWRIGHT,),
    "redirect_chase_then_soda": (REDIRECT_CHASE, SODA_ROW),
    "playwright_then_soda": (PLAYWRIGHT, SODA_ROW),
    "redirect_chase_then_playwright_then_soda": (REDIRECT_CHASE, PLAYWRIGHT, SODA_ROW),
}

# Canonical field_map names (jurisdictions.field_map) -> fields{} keys.
CANONICAL_FIELDS = {
    "permit_num": "permit_number",
    "status_current": "status",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip_code": "zip",
    "description": "description",
    "issue_date": "issued_date",
    "applied_at": "applied_date",
    "completed_at": "completed_date",
    "expires_at": "expires_date",
    "valuation": "valuation",
    "permit_class": "permit_class",
    "permit_class_mapped": "classification",
    "permit_type_mapped": "permit_type",
    "permit_type_desc": "permit_type_desc",
    "work_class": "work_class",
    "contractor_name": "contractor",
    "housing_category": "housing_category",
}

# Default portal label (case-insensitive, trailing ':' ignored) -> fields{} key.
DEFAULT_FIELD_LABELS = {
    "record number": "permit_number", "record id": "permit_number", "permit number": "permit_number",
    "permit #": "permit_number", "case number": "permit_number", "application number": "permit_number",
    "record status": "status", "status": "status", "permit status": "status",
    "work location": "address", "address": "address", "site address": "address",
    "project description": "description", "description": "description",
    "description of work": "description", "work description": "description",
    "scope of work": "scope", "scope": "scope",
    "permit issued date": "issued_date", "issued date": "issued_date", "issue date": "issued_date",
    "application completed date": "applied_date", "applied date": "applied_date",
    "application date": "applied_date",
    "expiration date": "expires_date", "expires": "expires_date",
    "total issued value": "valuation", "project value": "valuation", "valuation": "valuation",
    "job value": "valuation", "estimated cost": "valuation",
    "classification": "classification", "permit type": "permit_type", "record type": "permit_type",
    "trades": "trades", "trade": "trades", "inspections": "inspections",
}


@dataclass
class DetailConfig:
    jurisdiction: str
    plan: str
    strategies: Tuple[str, ...]
    allowed_hosts: List[str]
    soda_resource_url: Optional[str]
    soda_key_column: str
    min_signals: int = 4
    rate_limits_per_minute: Dict[str, float] = field(default_factory=dict)
    field_order: List[Tuple[str, str]] = field(default_factory=list)
    field_map: Dict[str, str] = field(default_factory=dict)
    portal_include: List[str] = field(default_factory=list)
    field_labels: Dict[str, str] = field(default_factory=dict)

    def allowed_hosts_for(self, detail_url: Optional[str]) -> List[str]:
        """Allow-list for a portal fetch; default: the detail URL's own host."""
        if self.allowed_hosts:
            return [h.lower() for h in self.allowed_hosts]
        host = urlsplit(detail_url or "").hostname
        return [host.lower()] if host else []

    def field_key(self, label: str) -> Optional[str]:
        norm = label.strip().rstrip(":").strip().lower()
        return self.field_labels.get(norm) or DEFAULT_FIELD_LABELS.get(norm)

    def catalog_columns(self) -> Dict[str, str]:
        """Source column -> fields{} key, from jurisdictions.field_map."""
        out = {}
        for canonical, column in self.field_map.items():
            key = CANONICAL_FIELDS.get(canonical)
            if key and column:
                out[column] = key
        return out


def parse_plan(plan: str) -> Tuple[str, ...]:
    if plan not in PLANS:
        raise ValueError(f"unknown detail fetch_strategy {plan!r}; expected one of {sorted(PLANS)}")
    return PLANS[plan]


def _soda_url(jurisdiction: Mapping[str, Any]) -> Optional[str]:
    base = (jurisdiction.get("base_url") or "").rstrip("/")
    dataset = jurisdiction.get("dataset_id")
    if (jurisdiction.get("platform_type") or "socrata") != "socrata" or not base or not dataset:
        return None
    return f"{base}/resource/{dataset}.json"


def load_config(jurisdiction: Mapping[str, Any]) -> DetailConfig:
    """Build a DetailConfig from a jurisdiction dict (id, base_url, dataset_id,
    platform_type, field_map, detail_strategy)."""
    block: Dict[str, Any] = dict((jurisdiction.get("detail_strategy") or {}).get("detail") or {})
    field_map = dict(jurisdiction.get("field_map") or {})
    plan = block.get("fetch_strategy", "soda_row")
    order: Sequence[Sequence[str]] = block.get("field_order") or []
    portal = block.get("portal") or {}
    return DetailConfig(
        jurisdiction=jurisdiction["id"],
        plan=plan,
        strategies=parse_plan(plan),
        allowed_hosts=list(block.get("allowed_hosts") or []),
        soda_resource_url=block.get("soda_resource_url") or _soda_url(jurisdiction),
        soda_key_column=block.get("soda_key_column") or field_map.get("permit_num") or "permitnum",
        min_signals=int(block.get("min_signals", 4)),
        rate_limits_per_minute={k.lower(): float(v) for k, v in (block.get("rate_limits_per_minute") or {}).items()},
        field_order=[(str(label), str(col)) for label, col in order],
        field_map=field_map,
        portal_include=list(portal.get("include") or []),
        field_labels={k.strip().rstrip(":").strip().lower(): v
                      for k, v in (portal.get("field_labels") or {}).items()},
    )

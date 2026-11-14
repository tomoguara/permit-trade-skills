"""Guess a jurisdiction's field map and status map from sample permit rows.

Used by local-file discovery. The guesses use the canonical names the ingest
normalizer reads (`permit_num`, `status_current`, `issue_date`, `valuation`, ...)
and the canonical status keys of `jurisdictions.status_map` (lower-case
`Status` values). Review the result before syncing; values the rules don't
recognise are left out and sync as UNKNOWN.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

# canonical field -> source column names to look for, in priority order (case-insensitive)
FIELD_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "permit_num": ("permitnum", "permit_number", "permitnumber", "permit_no", "permit_id"),
    "permit_class": ("permitclass", "permit_class"),
    "permit_class_mapped": ("permitclassmapped",),
    "permit_type_mapped": ("permittypemapped", "permit_type"),
    "permit_type_desc": ("permittypedesc", "permit_type_desc"),
    "status_current": ("statuscurrent", "status_current", "current_status", "permit_status", "status"),
    "description": ("description", "work_description", "project_description"),
    "address": ("originaladdress1", "address", "original_address", "site_address"),
    "city": ("originalcity", "city"),
    "state": ("originalstate", "state"),
    "zip_code": ("originalzip", "zip_code", "zipcode", "zip"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng"),
    "issue_date": ("issueddate", "issue_date", "issued_date", "date_issued"),
    "applied_at": ("applieddate", "applied_date", "application_date"),
    "expires_at": ("expiresdate", "expiration_date", "expires_date"),
    "completed_at": ("completeddate", "completed_date"),
    "valuation": ("estprojectcost", "valuation", "est_project_cost", "estimated_cost"),
    "contractor_name": ("contractorcompanyname", "contractorname", "contractor_name", "contractor"),
    "housing_units": ("housingunits", "housing_units"),
    "housing_units_added": ("housingunitsadded",),
    "housing_units_removed": ("housingunitsremoved",),
    "housing_category": ("housingcategory",),
    "work_class": ("workclass", "work_class"),
    "building_count": ("numbuildings", "building_count"),
    # The row's link to the official detail page (sync defaults to a `link` column).
    "detail_url": ("link", "detail_link", "detail_url", "permit_url", "record_url"),
}

# Evaluated in order; the first rule with a keyword contained in the
# lower-cased raw value wins. Order matters: "Application Completed" and
# "Reviews Completed" are pending (rule 3) before "completed" (rule 4).
STATUS_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("canceled", ("cancel", "withdr", "denied", "void", "revoked")),
    ("expired", ("expir",)),
    ("pending", ("pending", "review", "intake", "application", "applied", "initiated",
                 "scheduled", "submitted", "correction", "additional info", "awaiting",
                 "ready for", "in process", "received", "on hold")),
    ("completed", ("complete", "closed", "final", "occupy")),
    ("in_inspection", ("inspection",)),
    ("issued", ("issued", "active")),
)

SAMPLE_ROWS = 1000


def sample_columns(records: Sequence[Dict[str, Any]], sample: int = SAMPLE_ROWS) -> List[str]:
    """Column names seen in the first `sample` records, first-seen order, without `:` system fields.

    Socrata omits null fields per row, so one record is not enough.
    """
    seen: Dict[str, None] = {}
    for record in records[:sample]:
        if isinstance(record, dict):
            for key in record:
                if not key.startswith(":"):
                    seen.setdefault(key, None)
    return list(seen)


def guess_field_map(columns: Iterable[str]) -> Dict[str, str]:
    """canonical field -> source column, for every canonical field with a known synonym present."""
    by_lower = {c.lower(): c for c in columns}
    field_map: Dict[str, str] = {}
    for canonical, synonyms in FIELD_SYNONYMS.items():
        for name in synonyms:
            if name in by_lower:
                field_map[canonical] = by_lower[name]
                break
    return field_map


def classify_status(raw: str) -> str | None:
    """The canonical status key for a raw status value, or None if no rule matches."""
    value = raw.lower().strip()
    for canonical, keywords in STATUS_RULES:
        if any(k in value for k in keywords):
            return canonical
    return None


def guess_status_map(values: Iterable[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    """(status_map, unmatched): canonical key -> sorted raw values, and the raw values no rule matched."""
    status_map: Dict[str, List[str]] = {}
    unmatched: List[str] = []
    for raw in sorted({str(v) for v in values if v not in (None, "")}):
        canonical = classify_status(raw)
        if canonical is None:
            unmatched.append(raw)
        else:
            status_map.setdefault(canonical, []).append(raw)
    return status_map, unmatched

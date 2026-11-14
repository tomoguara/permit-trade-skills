"""Validate a jurisdiction's config before it is registered (Stage 01 R4).

Errors are problems that make sync store wrong data (every permit dropped,
every status UNKNOWN, broken detail URLs); `register` refuses them unless
`--force`. Warnings are worth a look but don't block.

The optional `columns` check compares mapped source columns with the
dataset's live `columns.json` (see `ingest.soda_client.fetch_columns`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from permit_trade_skills.discovery.mapping import FIELD_SYNONYMS
from permit_trade_skills.ingest.schemas import Status

# Canonical fields the ingest normalizer reads (field_map keys).
KNOWN_FIELDS = frozenset(FIELD_SYNONYMS) | {"detail_url", "applicant_name"}
# Unmapped -> warning (permit_num unmapped is an error: every row would be dropped).
CORE_FIELDS = ("status_current", "description", "issue_date", "address")
STATUS_KEYS = frozenset(s.value.lower() for s in Status)
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, List[str]]:
        return {"errors": self.errors, "warnings": self.warnings}


def validate_config(
    field_map: Dict[str, Any],
    status_map: Dict[str, Any],
    detail_strategy: Dict[str, Any],
    columns: Optional[Iterable[str]] = None,
) -> ValidationResult:
    """Check a field map, status map and detail strategy.

    Args:
        columns: the dataset's live column names; when given, every mapped
            source column must be among them.
    """
    result = ValidationResult()
    errors, warnings = result.errors, result.warnings
    field_map = field_map or {}
    status_map = status_map or {}
    detail_strategy = detail_strategy or {}

    # Field map
    bad_values = sorted(k for k, v in field_map.items() if not isinstance(v, str) or not v)
    for key in bad_values:
        errors.append(f"field_map[{key!r}] must be a source column name")
    if not field_map.get("permit_num"):
        errors.append("field_map has no 'permit_num': every row would be dropped (no permit number)")
    for key in sorted(set(field_map) - KNOWN_FIELDS):
        warnings.append(f"field_map key {key!r} is not a canonical field; sync ignores it")
    for key in CORE_FIELDS:
        if not field_map.get(key):
            warnings.append(f"canonical field {key!r} is not mapped")

    # Status map
    if not status_map:
        warnings.append("status_map is empty: every permit will have status UNKNOWN")
    seen: Dict[str, str] = {}
    for key, values in status_map.items():
        if key not in STATUS_KEYS:
            errors.append(f"status_map key {key!r} is not a canonical status "
                          f"(expected one of {sorted(STATUS_KEYS)})")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            errors.append(f"status_map[{key!r}] must be a list of raw status strings")
            continue
        for value in values:
            other = seen.setdefault(value.lower(), key)
            if other != key:
                warnings.append(f"raw status {value!r} is listed under both {other!r} and {key!r}; "
                                f"the first listed wins")

    # Detail strategy
    mapped_columns = {v for v in field_map.values() if isinstance(v, str)}
    rule = detail_strategy.get("detail_url_rule")
    if rule is not None:
        if not isinstance(rule, str):
            errors.append("detail_strategy.detail_url_rule must be a string")
        else:
            for column in _PLACEHOLDER.findall(rule):
                if column not in mapped_columns:
                    errors.append(f"detail_url_rule placeholder {{{{{column}}}}} is not a mapped "
                                  f"source column (field_map values)")
    timestamp_field = detail_strategy.get("timestamp_field")
    if not timestamp_field:
        warnings.append("detail_strategy.timestamp_field is not set: Socrata syncs always pull everything")
    elif timestamp_field not in mapped_columns:
        warnings.append(f"detail_strategy.timestamp_field {timestamp_field!r} is not a mapped source column")

    # Live columns
    if columns is not None:
        available = set(columns)
        for key, column in sorted(field_map.items()):
            if isinstance(column, str) and column not in available:
                errors.append(f"field_map[{key!r}] = {column!r} is not a column of the dataset")

    return result

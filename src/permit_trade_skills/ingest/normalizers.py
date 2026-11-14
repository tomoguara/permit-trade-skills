"""Field mapping and data normalization for permit ingestion."""

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from permit_trade_skills.ingest.schemas import PermitRecord, ProcessingState, Status

# {{column}} placeholders in detail_strategy.detail_url_rule
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def render_detail_url_rule(rule: str, raw_row: Dict[str, Any]) -> Optional[str]:
    """Fill a detail_url_rule's {{column}} placeholders from the raw row.

    Values are URL-encoded. Returns None if any placeholder's column is empty.
    """
    missing = False

    def sub(match: "re.Match[str]") -> str:
        nonlocal missing
        value = raw_row.get(match.group(1))
        if value in (None, ""):
            missing = True
            return ""
        return quote(str(value), safe="-_.~")

    url = _PLACEHOLDER.sub(sub, rule)
    return None if missing else url


def compute_raw_hash(row: Dict[str, Any]) -> str:
    """sha256 hex of the raw row, excluding Socrata ':'-prefixed metadata keys.

    Hashes Python's repr of the dict in source key order. raw_row is stored in a
    `json` (not `jsonb`) column precisely so key order survives the round trip
    and `pts db doctor` can recompute this value from the stored row.
    """
    clean_row = {k: v for k, v in row.items() if not k.startswith(':')}
    return hashlib.sha256(str(clean_row).encode()).hexdigest()


class PermitNormalizer:
    """Normalizes raw Socrata rows to canonical PermitRecord format."""
    
    def __init__(self, jurisdiction_id: str, jurisdiction_slug: str, field_map: Dict[str, str],
                 status_map: Dict[str, List[str]], detail_strategy: Dict[str, Any],
                 default_state: Optional[str] = None):
        """
        Initialize normalizer with jurisdiction configuration.

        Args:
            jurisdiction_id: The jurisdiction ID
            jurisdiction_slug: The jurisdiction slug
            field_map: Mapping from canonical field names to source column names
            status_map: Mapping from canonical status to list of raw status values
            detail_strategy: Detail fetching strategy configuration
                (`detail_url_rule` builds detail URLs for rows without a link)
            default_state: Used for `state` when the row has none (the
                jurisdiction's state). There is no city default.
        """
        self.jurisdiction_id = jurisdiction_id
        self.jurisdiction_slug = jurisdiction_slug
        self.field_map = field_map
        self.status_map = status_map
        self.detail_strategy = detail_strategy
        self.default_state = default_state
    
    def get_source_field(self, canonical_name: str) -> Optional[str]:
        """Get source field name for a canonical field."""
        return self.field_map.get(canonical_name, canonical_name)
    
    def map_status(self, raw_value: Optional[str]) -> Status:
        """Map raw status value to canonical Status enum."""
        if not raw_value:
            return Status.UNKNOWN
        
        raw_lower = str(raw_value).lower().strip()
        
        for canonical_status, raw_values in self.status_map.items():
            if any(str(v).lower() == raw_lower for v in raw_values):
                try:
                    return Status(canonical_status.upper())
                except ValueError:
                    return Status.UNKNOWN
        
        return Status.UNKNOWN
    
    # Text fields that are trimmed and have internal whitespace collapsed.
    # status_raw stays verbatim (audit); permit_number is only stripped (it is a key).
    TEXT_FIELDS = (
        'permit_class', 'permit_class_mapped', 'permit_type_mapped', 'permit_type_desc',
        'description', 'address', 'city', 'state', 'zip_code', 'work_class',
        'housing_category', 'contractor_name', 'applicant_name',
    )
    DATE_FIELDS = ('issue_date', 'applied_at', 'expires_at', 'completed_at')
    INT_FIELDS = ('building_count', 'housing_units', 'housing_units_added', 'housing_units_removed')

    def normalize(self, raw_row: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        """The canonical permit as the plain dict the sync upsert writes (see normalize_record)."""
        return self.normalize_record(raw_row, run_id).to_row()

    def normalize_record(self, raw_row: Dict[str, Any], run_id: str) -> PermitRecord:
        """
        Normalize a raw row to a canonical, validated PermitRecord.

        Values that can't be parsed become None with a per-row warning
        (`unparseable_date` / `unparseable_number`); nothing is dropped here.

        Args:
            raw_row: Raw Socrata row dictionary
            run_id: Sync run ID for provenance
        """
        warnings: List[Dict[str, Any]] = []
        now = datetime.utcnow()

        status_raw = raw_row.get(self.get_source_field('status_current'))
        if status_raw is not None and not isinstance(status_raw, str):
            status_raw = str(status_raw)
        status = self.map_status(status_raw)
        if status == Status.UNKNOWN:
            warnings.append({"type": "unknown_status", "raw": status_raw})

        dates = {name: self._date(raw_row, name, warnings) for name in self.DATE_FIELDS}
        latitude = self._number(raw_row, 'latitude', warnings)
        longitude = self._number(raw_row, 'longitude', warnings)
        if (latitude is not None and not -90 <= latitude <= 90) or (
            longitude is not None and not -180 <= longitude <= 180
        ):
            warnings.append({"type": "invalid_coordinates", "latitude": latitude, "longitude": longitude})
            latitude = longitude = None
        detail_url = self._build_detail_url(raw_row)
        if detail_url is None:
            warnings.append({"type": "missing_detail_url"})

        # Provenance: the source's row timestamp, else the issue date, else a warning.
        source_updated_at = self._parse_timestamp(raw_row, ':updated_at') or dates['issue_date']
        if source_updated_at is None:
            warnings.append({"type": "missing_source_updated_at"})

        text = {name: self._text(raw_row, name) for name in self.TEXT_FIELDS}
        if text['state'] is None:
            text['state'] = self.default_state
        permit_number = raw_row.get(self.get_source_field('permit_num'))
        if permit_number is not None:
            permit_number = str(permit_number).strip() or None

        return PermitRecord(
            jurisdiction_id=self.jurisdiction_id,
            jurisdiction_slug=self.jurisdiction_slug,
            permit_number=permit_number,
            status_raw=status_raw,
            status=status,
            status_map_key=self._get_status_map_key(raw_row),
            issued_at=dates['issue_date'],  # For Phase 1, same as issue_date
            latitude=latitude,
            longitude=longitude,
            valuation=self._number(raw_row, 'valuation', warnings),
            **dates,
            **text,
            **{name: self._number(raw_row, name, warnings, integer=True) for name in self.INT_FIELDS},
            raw_row=raw_row,
            raw_hash=self._compute_hash(raw_row),
            row_id_source=self._as_str(raw_row.get(':id')),
            row_version_source=self._as_str(raw_row.get(':version')),
            warnings=warnings,
            source_updated_at=source_updated_at,
            source_synced_at=now,
            detail_url=detail_url,
            detail_strategy=self.detail_strategy.get('type', 'dataset_api'),
            detail_fetch_type=self.detail_strategy.get('method', 'GET'),
            run_id=run_id,
            created_at=now,
            updated_at=now,
            synced_at=now,
            processing_state=ProcessingState.SYNCED,
        )

    @staticmethod
    def _as_str(value: Any) -> Optional[str]:
        return None if value is None else str(value)

    def _text(self, raw_row: Dict[str, Any], canonical: str) -> Optional[str]:
        """Trimmed text with internal whitespace collapsed; empty -> None. Case is kept."""
        value = raw_row.get(self.get_source_field(canonical))
        if value is None:
            return None
        return ' '.join(str(value).split()) or None

    def _date(self, raw_row: Dict[str, Any], canonical: str,
              warnings: List[Dict[str, Any]]) -> Optional[datetime]:
        column = self.get_source_field(canonical)
        value = raw_row.get(column)
        parsed = self._parse_timestamp(raw_row, column)
        if parsed is None and value not in (None, ''):
            warnings.append({"type": "unparseable_date", "field": canonical, "value": str(value)[:100]})
        return parsed

    def _number(self, raw_row: Dict[str, Any], canonical: str, warnings: List[Dict[str, Any]],
                integer: bool = False):
        column = self.get_source_field(canonical)
        value = raw_row.get(column)
        parsed = (self._parse_int if integer else self._parse_float)(raw_row, column)
        if parsed is None and value not in (None, ''):
            warnings.append({"type": "unparseable_number", "field": canonical, "value": str(value)[:100]})
        return parsed

    def _parse_timestamp(self, raw_row: Dict[str, Any], field_name: str) -> Optional[datetime]:
        """Parse a timestamp field from raw row."""
        value = raw_row.get(field_name)
        if not value:
            return None
        
        try:
            # Handle various formats
            if isinstance(value, str):
                # Remove timezone suffix if present
                value = value.replace('Z', '').replace('+00:00', '')
                # Try parsing ISO format
                if 'T' in value:
                    try:
                        return datetime.fromisoformat(value)
                    except ValueError:
                        pass
                # Try parsing date format
                try:
                    return datetime.strptime(value.split('T')[0], '%Y-%m-%d')
                except ValueError:
                    pass
            return None
        except Exception:
            return None
    
    def _parse_float(self, raw_row: Dict[str, Any], field_name: str) -> Optional[float]:
        """Parse a float field from raw row."""
        value = raw_row.get(field_name)
        if value is None:
            return None
        
        try:
            if isinstance(value, (int, float)):
                return float(value)
            elif isinstance(value, str):
                return float(value.replace(',', '').strip())
        except (ValueError, AttributeError):
            pass
        
        return None
    
    def _parse_int(self, raw_row: Dict[str, Any], field_name: str) -> Optional[int]:
        """Parse an integer field from raw row."""
        value = raw_row.get(field_name)
        if value is None:
            return None
        
        try:
            if isinstance(value, int):
                return value
            elif isinstance(value, float):
                return int(value)
            elif isinstance(value, str):
                # Socrata serialises some integer columns as "1.0"
                return int(float(value.replace(',', '').strip()))
        except (ValueError, AttributeError):
            pass
        
        return None
    
    def _get_status_map_key(self, raw_row: Dict[str, Any]) -> Optional[str]:
        """Get the status_map key that matched the raw status."""
        status_raw = raw_row.get(self.get_source_field('status_current'))
        if not status_raw:
            return None
        
        raw_lower = str(status_raw).lower().strip()
        
        for canonical_status, raw_values in self.status_map.items():
            if any(str(v).lower() == raw_lower for v in raw_values):
                return canonical_status
        
        return None
    
    def _build_detail_url(self, raw_row: Dict[str, Any]) -> Optional[str]:
        """The row's link (field_map["detail_url"], default column `link`), else
        detail_strategy.detail_url_rule rendered from the row, else None."""
        link = raw_row.get(self.field_map.get('detail_url', 'link'))
        if isinstance(link, dict) and link.get('url'):
            return link['url']
        if isinstance(link, str) and 'http' in link:
            return link

        rule = self.detail_strategy.get('detail_url_rule')
        if rule:
            return render_detail_url_rule(rule, raw_row)
        return None
    
    def _compute_hash(self, row: Dict[str, Any]) -> str:
        """Compute hash of raw row for idempotency."""
        return compute_raw_hash(row)

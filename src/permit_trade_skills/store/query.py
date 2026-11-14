"""PermitQuery: the structured permit query, plus keyset (cursor) paging.

Paging is keyset, never OFFSET: each sort orders by (sort column, id) and the
next page is "rows strictly after the last row's (value, id)". NULL sort values
are placed last for descending sorts, and the cursor encodes whether the last
row's value was NULL so paging continues correctly through the NULL tail.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, List, Optional, Sequence, TypeVar, Union

MAX_PAGE_SIZE = 200
CONFIDENCE_LEVELS = ("low", "medium", "high")   # ascending; same as skills.schemas
TRADE_MATCHES = ("any", "all")


class PermitSort(str, Enum):
    ISSUED_AT_DESC = "issued_at_desc"
    APPLIED_DESC = "applied_desc"
    COST_DESC = "cost_desc"
    NUMBER_ASC = "number_asc"


# sort -> (column name, descending?)
SORT_COLUMNS = {
    PermitSort.ISSUED_AT_DESC: ("issued_at", True),
    PermitSort.APPLIED_DESC: ("applied_at", True),
    PermitSort.COST_DESC: ("valuation", True),
    PermitSort.NUMBER_ASC: ("permit_number", False),
}

StrOrList = Union[str, Sequence[str], None]


@dataclass
class PermitQuery:
    """Structured permit query. Only `jurisdiction` (slug) is required.

    status / statuses_exclude / permit_class / permit_type / state accept a
    single value or a list. permit_class matches permit_class or
    permit_class_mapped case-insensitively (e.g. "Residential", "Commercial").
    desc_keyword is a case-insensitive substring match on description (ILIKE,
    served by the trigram GIN index). city is a case-insensitive substring
    match (ILIKE) on the permit's address city.

    trade matches permits whose latest skill inference (if not REJECTED) lists a
    skill in the given trades (case-insensitive, e.g. "Carpentry"): any of them
    (trade_match None or "any") or every one ("all"). Each matching skill needs
    at least trade_min_confidence ("low" | "medium" | "high") when set. Permits
    with no inference never match; QueryService reports that coverage.
    """

    jurisdiction: str
    status: StrOrList = None
    statuses_exclude: StrOrList = None
    permit_class: StrOrList = None
    permit_type: StrOrList = None
    desc_keyword: Optional[str] = None
    trade: StrOrList = None
    trade_min_confidence: Optional[str] = None
    trade_match: Optional[str] = None   # "any" (default when None) | "all"
    applied_after: Optional[datetime] = None
    applied_before: Optional[datetime] = None
    issued_after: Optional[datetime] = None
    issued_before: Optional[datetime] = None
    zip: Optional[str] = None
    city: Optional[str] = None
    min_cost: Optional[float] = None
    max_cost: Optional[float] = None
    state: StrOrList = None  # lifecycle (processing_state)
    sort: PermitSort = PermitSort.ISSUED_AT_DESC
    page_size: int = 25
    page: Optional[int] = None      # 1-based; walks keyset cursors (no OFFSET)
    cursor: Optional[str] = None    # opaque token from CursorPage.next_cursor

    def __post_init__(self) -> None:
        if not self.jurisdiction:
            raise ValueError("PermitQuery.jurisdiction is required")
        self.sort = PermitSort(self.sort)
        if self.trade_min_confidence is not None and self.trade_min_confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"trade_min_confidence must be one of {', '.join(CONFIDENCE_LEVELS)}")
        if self.trade_match is not None and self.trade_match not in TRADE_MATCHES:
            raise ValueError(f"trade_match must be one of {', '.join(TRADE_MATCHES)}")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if self.page is not None and self.page < 1:
            raise ValueError("page is 1-based")
        if self.page is not None and self.cursor is not None:
            raise ValueError("pass either page or cursor, not both")

    def to_dict(self) -> dict:
        """The query as JSON-ready data: set fields only (None omitted), dates ISO-8601.

        This is the `query` echoed in API/tool responses: exactly what was executed.
        """
        out = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, (datetime, date)):
                value = value.isoformat()
            elif isinstance(value, (list, tuple, set)):
                value = list(value)
            out[name] = value
        return out


T = TypeVar("T")


@dataclass
class CursorPage(Generic[T]):
    rows: List[T]
    next_cursor: Optional[str]  # None on the last page
    total_estimate: int         # planner estimate of matching rows (not an exact count)


@dataclass(frozen=True)
class Cursor:
    sort: str
    value: Any        # last row's sort value (JSON-encodable), or None
    is_null: bool     # last row's sort value was NULL
    id: int           # last row's id (tie-breaker)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"__dt__": value.isoformat()}
    return value


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict) and "__dt__" in value:
        return datetime.fromisoformat(value["__dt__"])
    return value


def encode_cursor(sort: PermitSort, value: Any, row_id: int) -> str:
    payload = {"s": sort.value, "v": _jsonable(value), "n": value is None, "i": row_id}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(token: str) -> Cursor:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()))
        return Cursor(payload["s"], _from_jsonable(payload["v"]), bool(payload["n"]), int(payload["i"]))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid cursor: {e}") from e

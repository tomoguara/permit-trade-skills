"""Parse loosely-typed query parameters (REST query strings, tool JSON, NLU output)
into a validated `PermitQuery`.

Parameter names are the `PermitQuery` field names. Lists may be given as a
list or a comma-separated string. Dates are ISO-8601 dates or datetimes
(naive = UTC). `page_size` is clamped to 1..200.

`strict=True` (REST, tools) raises `QueryError` on the first bad parameter.
`strict=False` (NLU) drops bad parameters and reports each one, so the caller
can list them under `unfulfilled_filters` instead of silently ignoring them.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from permit_trade_skills.ingest.schemas import ProcessingState, Status
from permit_trade_skills.query.errors import (
    INVALID_DATE_RANGE,
    INVALID_PARAMETER,
    QueryError,
)
from permit_trade_skills.store.query import (
    CONFIDENCE_LEVELS,
    MAX_PAGE_SIZE,
    TRADE_MATCHES,
    PermitQuery,
    PermitSort,
)

STATUSES = [s.value for s in Status]
STATES = [s.value for s in ProcessingState]
SORTS = [s.value for s in PermitSort]

LIST_PARAMS = ("status", "statuses_exclude", "permit_class", "permit_type", "trade", "state")
STR_PARAMS = ("desc_keyword", "zip", "city", "cursor")
DATE_PARAMS = ("applied_after", "applied_before", "issued_after", "issued_before")
COST_PARAMS = ("min_cost", "max_cost")
FILTER_PARAMS = LIST_PARAMS + ("desc_keyword", "trade_min_confidence", "trade_match", "zip", "city") + DATE_PARAMS + COST_PARAMS
ALL_PARAMS = FILTER_PARAMS + ("sort", "page_size", "page", "cursor")
DATE_PAIRS = (("applied_after", "applied_before"), ("issued_after", "issued_before"))


def parse_date(value: Any) -> datetime:
    """ISO date or datetime (string, date or datetime) -> naive UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"expected an ISO date, got {value!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ValueError(f"expected a string or list, got {value!r}")
    out = [str(v).strip() for v in items if str(v).strip()]
    if not out:
        raise ValueError("empty list")
    return out


def _parse_one(name: str, value: Any) -> Any:
    if name in ("status", "statuses_exclude"):
        values = [v.upper() for v in _as_list(value)]
        bad = [v for v in values if v not in STATUSES]
        if bad:
            raise ValueError(f"unknown status {', '.join(bad)} (expected {', '.join(STATUSES)})")
        return values
    if name == "state":
        values = [v.upper() for v in _as_list(value)]
        bad = [v for v in values if v not in STATES]
        if bad:
            raise ValueError(f"unknown lifecycle state {', '.join(bad)} (expected {', '.join(STATES)})")
        return values
    if name in ("permit_class", "permit_type", "trade"):
        return _as_list(value)
    if name == "trade_min_confidence":
        if value not in CONFIDENCE_LEVELS:
            raise ValueError(f"expected one of {', '.join(CONFIDENCE_LEVELS)}")
        return value
    if name == "trade_match":
        if value not in TRADE_MATCHES:
            raise ValueError(f"expected one of {', '.join(TRADE_MATCHES)}")
        return value
    if name in DATE_PARAMS:
        return parse_date(value)
    if name in COST_PARAMS:
        if isinstance(value, bool):
            raise ValueError(f"expected a number, got {value!r}")
        number = float(value)
        if number < 0:
            raise ValueError("must be >= 0")
        return number
    if name == "sort":
        if value not in SORTS:
            raise ValueError(f"expected one of {', '.join(SORTS)}")
        return PermitSort(value)
    if name == "page_size":
        return max(1, min(MAX_PAGE_SIZE, int(value)))
    if name == "page":
        page = int(value)
        if page < 1:
            raise ValueError("page is 1-based")
        return page
    if name in STR_PARAMS:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"expected a non-empty string, got {value!r}")
        return value.strip()
    raise ValueError("unknown parameter")


def parse_params(jurisdiction: str, params: Mapping[str, Any], *,
                 strict: bool = True) -> Tuple[PermitQuery, List[str]]:
    """Build a PermitQuery from `params`. Returns (query, problems).

    None values are treated as absent. In strict mode the first problem raises
    QueryError(INVALID_PARAMETER | INVALID_DATE_RANGE); otherwise problems are
    returned as human-readable strings ("status=CLOSED: unknown status ...").
    """
    kwargs: Dict[str, Any] = {}
    problems: List[str] = []

    def problem(code: str, text: str) -> None:
        if strict:
            raise QueryError(code, text)
        problems.append(text)

    for name, value in params.items():
        if value is None:
            continue
        if name not in ALL_PARAMS:
            problem(INVALID_PARAMETER, f"{name}={value!r}: unknown parameter")
            continue
        try:
            kwargs[name] = _parse_one(name, value)
        except (TypeError, ValueError) as e:
            problem(INVALID_PARAMETER, f"{name}={value!r}: {e}")

    for start, end in DATE_PAIRS + (("min_cost", "max_cost"),):
        if start in kwargs and end in kwargs:
            invalid = (kwargs[start] >= kwargs[end]) if start in DATE_PARAMS else (kwargs[start] > kwargs[end])
            if invalid:
                problem(INVALID_DATE_RANGE if start in DATE_PARAMS else INVALID_PARAMETER,
                        f"{start}={params[start]!r} must be before {end}={params[end]!r}")
                kwargs.pop(start), kwargs.pop(end)

    try:
        return PermitQuery(jurisdiction=jurisdiction, **kwargs), problems
    except ValueError as e:  # e.g. page together with cursor
        raise QueryError(INVALID_PARAMETER, str(e)) from e


def filters_only(params: Mapping[str, Any]) -> Dict[str, Any]:
    """The filter subset of params (drops sort/paging and None values)."""
    return {k: v for k, v in params.items() if k in FILTER_PARAMS and v is not None}


def check_window(since: Optional[datetime], until: Optional[datetime]) -> None:
    if since is not None and until is not None and since >= until:
        raise QueryError(INVALID_DATE_RANGE, f"since ({since.date()}) must be before until ({until.date()})")

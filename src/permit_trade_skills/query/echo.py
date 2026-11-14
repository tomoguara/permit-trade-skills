"""Plain-English echo of an executed PermitQuery.

The echo is rendered from the executed query by a fixed template; it is never
written by an LLM. If the NLU drops or misreads a filter, the echo shows what
was actually run, so the user can see the mismatch.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from permit_trade_skills.store.query import PermitQuery, PermitSort

DEFAULT_PAGE_SIZE = PermitQuery.__dataclass_fields__["page_size"].default

SORT_LABELS = {
    PermitSort.ISSUED_AT_DESC: "issue date (newest first)",
    PermitSort.APPLIED_DESC: "application date (newest first)",
    PermitSort.COST_DESC: "est. cost (highest first)",
    PermitSort.NUMBER_ASC: "permit number",
}


def _list(value) -> List[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _or(values: List[str]) -> str:
    return values[0] if len(values) == 1 else ", ".join(values[:-1]) + " or " + values[-1]


def _and(values: List[str]) -> str:
    return values[0] if len(values) == 1 else ", ".join(values[:-1]) + " and " + values[-1]


def _day(dt: datetime) -> str:
    if dt.hour == dt.minute == dt.second == dt.microsecond == 0:
        return dt.date().isoformat()
    return dt.isoformat(timespec="minutes")


def _money(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def render_echo(q: PermitQuery, *, suffix: Optional[str] = None) -> str:
    statuses = [s.upper() for s in _list(q.status)]
    first = f"the first {q.page_size} " if q.page_size != DEFAULT_PAGE_SIZE and not q.cursor else ""
    head = f"Showing {first}{_or(statuses) + ' ' if statuses else ''}permits"
    parts: List[str] = []
    if q.statuses_exclude:
        parts.append(f"excluding {_or([s.upper() for s in _list(q.statuses_exclude)])}")
    if q.permit_class:
        parts.append(f"{_or(_list(q.permit_class))} class")
    if q.permit_type:
        parts.append(f"{_or(_list(q.permit_type))} type")
    if q.desc_keyword:
        parts.append(f'description contains "{q.desc_keyword}"')
    if q.trade:
        conf = f" (confidence {q.trade_min_confidence} or higher)" if q.trade_min_confidence else ""
        join = _and if q.trade_match == "all" else _or
        parts.append(f"needing {join(_list(q.trade))} work per inferred trade skills{conf}")
    if q.city:
        parts.append(f'city matches "{q.city}"')
    if q.zip:
        parts.append(f"ZIP {q.zip}")
    for label, after, before in (("applied", q.applied_after, q.applied_before),
                                 ("issued", q.issued_after, q.issued_before)):
        if after is not None:
            parts.append(f"{label} on or after {_day(after)}")
        if before is not None:
            parts.append(f"{label} before {_day(before)}")
    if q.min_cost is not None:
        parts.append(f"est. cost >= {_money(q.min_cost)}")
    if q.max_cost is not None:
        parts.append(f"est. cost <= {_money(q.max_cost)}")
    if q.state:
        parts.append(f"lifecycle state {_or(_list(q.state))}")

    text = head + (", " + ", ".join(parts) if parts else "")
    text += f" in {q.jurisdiction}, sorted by {SORT_LABELS[q.sort]}"
    if suffix:
        text += f" {suffix}"
    return text + "."

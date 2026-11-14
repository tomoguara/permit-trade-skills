"""A fake Socrata (SODA 2.1) server for offline ingestion tests.

`FakeSocrata(rows)` is an httpx.MockTransport handler serving one dataset:

- `GET .../resource/<id>.json` with `$where`, `$order`, `$limit`, `$offset`,
  and `$select=count(*) AS count` (no $group) for `SodaClient.count()`;
- `GET .../api/views/<id>/columns.json` (field names: `columns`, default every
  key seen in the rows).

`$where` supports `col > 'v'`, `col >= 'v'` and `OR`-joined, parenthesised
clauses, comparing like Socrata's floating timestamps ('2026-02-19T00:00' is
not > '2026-02-19'). Every request is kept in `requests`.

Use the `make_soda_page` fixture (tests/conftest.py) to build one.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote

import httpx

_CLAUSE = re.compile(r"^\(?\s*(\S+)\s*(>=|>)\s*'([^']*)'\s*\)?$")


def _matches(row: Dict[str, Any], where: str) -> bool:
    for clause in where.split(" OR "):
        m = _CLAUSE.match(clause.strip())
        if not m:
            raise ValueError(f"FakeSocrata can't parse $where clause {clause!r}")
        column, op, value = m.groups()
        cell = str(row.get(column) or "")[:len(value)]
        if cell and (cell > value if op == ">" else cell >= value):
            return True
    return False


class FakeSocrata:
    def __init__(self, rows: List[Dict[str, Any]], columns: Optional[Iterable[str]] = None,
                 fail_on_page: Optional[int] = None, status: int = 500,
                 columns_status: int = 200):
        self.rows = rows
        self.columns = list(columns) if columns is not None else \
            sorted({k for r in rows for k in r if not k.startswith(":")})
        self.fail_on_page, self.status, self.columns_status = fail_on_page, status, columns_status
        self.requests: List[httpx.Request] = []

    @property
    def row_requests(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/resource/")]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/columns.json"):
            if self.columns_status != 200:
                return httpx.Response(self.columns_status)
            return httpx.Response(200, json=[{"fieldName": c} for c in self.columns])

        p = request.url.params
        rows = self.rows
        if "$where" in p:
            where = unquote(p["$where"])
            rows = [r for r in rows if _matches(r, where)]
        if p.get("$select", "").lower().startswith("count(") and "$limit" not in p:
            return httpx.Response(200, json=[{"count": str(len(rows))}])

        page = int(p["$offset"]) // int(p["$limit"]) + 1
        if self.fail_on_page == page:
            return httpx.Response(self.status)
        if "$order" in p:
            col, _, direction = p["$order"].partition(" ")
            rows = sorted(rows, key=lambda r: r.get(col) or "", reverse=direction == "DESC")
        offset, limit = int(p["$offset"]), int(p["$limit"])
        return httpx.Response(200, json=rows[offset:offset + limit])

"""Choosing a live permit for the opt-in E2E check (`pts detail pick-live`)."""

from __future__ import annotations

from typing import List


def pick_live_permits(query_service, jurisdiction: str, count: int = 1) -> List[str]:
    """Most recently issued ISSUED permits that carry a detail URL, via the Stage 04 query API."""
    result = query_service.search(jurisdiction, {"status": ["ISSUED"], "sort": "issued_at_desc",
                                                 "page_size": max(count * 4, 10)})
    picked = [r["permit_number"] for r in result["rows"] if r.get("detail_url")]
    return picked[:count]

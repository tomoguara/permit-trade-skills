"""Stage 05: permit detail extraction.

For a permit with a detail URL (or a Socrata row), fetch the official record,
validate it belongs to the permit (F2 signals), normalize it into a stable,
citable `content_text` + structured `fields`, and store a `detail_snapshots`
row. Stage 06 (skills) cites `content_text`; Stage 07 calls
`DetailService.ensure_fresh`.

Modules: config (registry block), models, normalize (pure), signals (F2),
fetchers (network), ratelimit, robots, judge (optional LLM), pipeline,
runs (ledger report), service, ports (persistence Protocol).

This package never imports `permit_trade_skills.store`; build a wired service
with `permit_trade_skills.wiring.detail_service()`. See docs/detail.md.
"""

from permit_trade_skills.detail.models import DETAIL_DOCUMENT_KEYS, DetailDocument, FetchResult
from permit_trade_skills.detail.service import DetailService, DetailUnavailable, PermitNotFound

__all__ = ["DETAIL_DOCUMENT_KEYS", "DetailDocument", "DetailService", "DetailUnavailable", "FetchResult",
           "PermitNotFound"]

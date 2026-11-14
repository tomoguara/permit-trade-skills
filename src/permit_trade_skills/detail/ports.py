"""The persistence port of the detail stage.

`detail/` never imports `permit_trade_skills.store` (Stage 04 boundary,
tests/unit/test_import_boundary.py). It talks to this Protocol; the concrete
implementation is `permit_trade_skills.store.details.SqlDetailStore`, wired in
by the composition root (`permit_trade_skills.wiring.detail_service`) or by tests.
All values are plain dicts / scalars, so the store doesn't depend on `detail/`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from permit_trade_skills.detail.models import PermitRef

PERMIT_KEYS = ("id", "jurisdiction_id", "permit_number", "detail_url", "raw_row", "raw_hash", "status",
               "processing_state", "address", "description", "issued_at", "applied_at", "completed_at",
               "expires_at", "valuation")


class DetailStore(Protocol):
    def jurisdiction(self, slug: str) -> Optional[Dict[str, Any]]:
        """id, platform_type, base_url, dataset_id, field_map, detail_strategy."""

    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        """PERMIT_KEYS for one permit."""

    def select_permits(self, jurisdiction: str, *, status: Optional[str], since: Optional[datetime],
                       until: Optional[datetime], limit: int,
                       permit_numbers: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Cohort for a batch, newest issued first. `since`/`until` filter issued_at."""

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]:
        """Latest detail_snapshots row as a dict (all columns)."""

    def save_snapshot(self, permit_id: int, document: Dict[str, Any], *, raw_html: Optional[str],
                      raw_responses: Optional[List[Dict[str, Any]]], http_status: Optional[int],
                      source_hash: Optional[str], run_id: Optional[str]) -> Tuple[int, bool, datetime]:
        """Insert, or refresh the row with the same (permit_id, content_hash).
        Returns (snapshot_id, inserted, fetched_at)."""

    def mark_detail_state(self, permit_id: int, ok: bool,
                          error: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """ok: SYNCED/DETAIL_FAILED/DETAIL_READY -> DETAIL_READY. Failure: SYNCED/DETAIL_FAILED ->
        DETAIL_FAILED with state_error (a permit with a usable snapshot keeps its state).
        Returns the new state, or None when unchanged."""

    def create_run(self, run_id: str, jurisdiction: str, trigger: str, params: Dict[str, Any]) -> None: ...

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...

    def record_result(self, run_id: str, result: Dict[str, Any]) -> None:
        """Upsert one detail_fetch_results row (keyed on run_id, permit_id)."""

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        """Recount ok/failed/skipped from the results, close the run, return it."""

    def run_results(self, run_id: str) -> List[Dict[str, Any]]: ...

    def done_permit_ids(self, run_id: str) -> Set[int]:
        """Permits with outcome ok or skipped in the run (for --resume)."""


def permit_ref(row: Dict[str, Any]) -> PermitRef:
    return PermitRef(
        permit_id=row["id"], jurisdiction=row["jurisdiction_id"], permit_number=row["permit_number"],
        detail_url=row.get("detail_url"), raw_row=dict(row.get("raw_row") or {}),
        raw_hash=row.get("raw_hash"), status=row.get("status"),
        processing_state=row.get("processing_state"), address=row.get("address"),
        description=row.get("description"), issued_at=row.get("issued_at"),
        applied_at=row.get("applied_at"), completed_at=row.get("completed_at"),
        expires_at=row.get("expires_at"), valuation=row.get("valuation"),
    )

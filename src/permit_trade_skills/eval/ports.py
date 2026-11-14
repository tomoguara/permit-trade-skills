"""Persistence port of the evaluation stage, implemented by `store/eval.SqlEvalStore`.

Plain dicts in and out; datetimes are naive UTC. `eval/` never imports the
store (tests/unit/test_import_boundary.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set


class EvalStore(Protocol):
    # ---- reference data ----------------------------------------------------
    def active_jurisdictions(self) -> List[str]: ...

    def jurisdiction(self, slug: str) -> Optional[Dict[str, Any]]:
        """id, name, county, state, field_map, status_map, detail_strategy, base_url, dataset_id, platform_type."""

    def permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        """Catalog fields (Stage 06 PermitInput fields) + raw_row, raw_hash, status_raw, created_at."""

    def snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]: ...

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]: ...

    def recent_snapshots(self, jurisdiction: str, *, limit: Optional[int] = None,
                         strategy: Optional[str] = None, since: Optional[datetime] = None,
                         latest_only: bool = True) -> List[Dict[str, Any]]:
        """Snapshots joined with permit_number, address, status_raw, raw_row; newest first."""

    # ---- inferences, events ------------------------------------------------
    def accepted_inferences(self, jurisdiction: str, *, since: Optional[datetime] = None,
                            limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """skill_inferences rows + permit_number, detail_url, snapshot content_text/final_url (when stored)."""

    def agent_runs(self, *, since: Optional[datetime] = None, limit: Optional[int] = None) -> List[str]: ...

    def run_events(self, run_id: str) -> List[Dict[str, Any]]: ...

    def illegal_transitions(self, *, since: Optional[datetime] = None) -> List[Dict[str, Any]]: ...

    def self_approvals(self, jurisdiction: str) -> List[Dict[str, Any]]: ...

    # ---- results -------------------------------------------------------------
    def cached_payload(self, cache_key: str) -> Optional[Dict[str, Any]]: ...

    def save_results(self, rows: Sequence[Dict[str, Any]]) -> None: ...

    def latest_summary(self, suite: str, *, build_id: Optional[str] = None,
                       jurisdiction: Optional[str] = None) -> Optional[Dict[str, Any]]: ...

    def summaries(self, suite: Optional[str] = None, *, since: Optional[datetime] = None,
                  limit: int = 50) -> List[Dict[str, Any]]: ...

    def items(self, suite: str, *, run_id: Optional[str] = None, since: Optional[datetime] = None,
              failed_only: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...

    # ---- labels ----------------------------------------------------------------
    def add_label(self, row: Dict[str, Any], *, replaces: Optional[int] = None) -> int: ...

    def reviewers(self, permit_id: int) -> Set[str]:
        """Names that reviewed (approved/rejected/flagged) this permit's inferences."""

    def current_labels(self, jurisdiction: str, kind: str, item_key: Optional[str] = None,
                       role: Optional[str] = None) -> List[Dict[str, Any]]: ...

    def supersede_label(self, label_id: int, by_id: Optional[int]) -> None: ...

    # ---- gate, alerts ------------------------------------------------------------
    def save_gate(self, row: Dict[str, Any]) -> int: ...

    def latest_gate(self, build_id: str) -> Optional[Dict[str, Any]]: ...

    def sign_off_gate(self, gate_id: int, by: str, note: str) -> bool: ...

    def add_alert(self, row: Dict[str, Any]) -> int: ...

    def alerts(self, *, since: Optional[datetime] = None, detector: Optional[str] = None,
               open_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]: ...

    def mark_alert(self, alert_id: int, *, notified: Optional[bool] = None,
                   acknowledged_by: Optional[str] = None) -> bool: ...

    # ---- monitoring queries ------------------------------------------------------
    def confidence_counts(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, Any]:
        """{"counts": {high, medium, low}, "models": {model@prompt_version: n}}."""

    def unknown_status_share(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, int]:
        """{"unknown": n, "total": n} over permits written (created or updated) in the window."""

    def degraded_share(self, jurisdiction: str, start: datetime, end: datetime) -> Dict[str, int]:
        """{"degraded": n, "total": n} over detail snapshots fetched in the window."""

    def slo_rows(self, jurisdiction: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """ISSUED permits first synced in [start, end): created_at, issued_at, first_inference_at."""

    def count_query(self, query: Any) -> int:
        """Exact row count of a Stage 04 PermitQuery's filters."""

    def permit_numbers_present(self, jurisdiction: str, numbers: Iterable[str]) -> Set[str]: ...

    def status_distribution(self, jurisdiction: str) -> Dict[str, int]: ...

    def latest_sync(self, jurisdiction: str) -> Optional[Dict[str, Any]]: ...

    def metric_counts(self, jurisdiction: Optional[str]) -> Dict[str, Any]:
        """Counters for /metrics (see eval/metrics.py)."""

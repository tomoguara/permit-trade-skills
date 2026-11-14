"""The persistence port of the orchestration stage.

`orchestration/` never imports `permit_trade_skills.store` (Stage 04 boundary,
tests/unit/test_import_boundary.py). It talks to this Protocol; the SQL
implementation is `permit_trade_skills.store.orchestration.SqlOrchestrationStore`,
wired by `permit_trade_skills.wiring`. Values are plain dicts / scalars.

Detail fetching and skill inference are not here: the worker and the tools
call the Stage 05 `DetailService` and Stage 06 `SkillService`, which own their
own state writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple


class OrchestrationStore(Protocol):
    # ---- events / runs -----------------------------------------------------

    def add_event(self, event: Dict[str, Any]) -> None:
        """Append one orchestration_events row (keys = column names; created_at defaults to now)."""

    def run_events(self, run_id: str) -> List[Dict[str, Any]]:
        """All events of a run, oldest first."""

    def create_pipeline_run(self, run_id: str, jurisdiction: str, trigger: str, params: Dict[str, Any]) -> None: ...

    def finish_pipeline_run(self, run_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        """Write the run report columns (status, counts, llm_calls, tokens, est_cost, error, ...)."""

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...

    def recent_pipeline_runs(self, jurisdiction: Optional[str], limit: int) -> List[Dict[str, Any]]: ...

    # ---- permits and the durable queue ------------------------------------

    def active_jurisdictions(self) -> List[str]: ...

    def eligible_permits(self, jurisdiction: str, *, statuses: Sequence[str], states: Sequence[str],
                         since: Optional[datetime], permit_numbers: Optional[Sequence[str]], limit: int,
                         now: datetime, ignore_backlog: bool = False) -> List[Dict[str, Any]]:
        """Permits to process, newest issued first.

        Rows: id, permit_number, status, processing_state, state_error, issued_at,
        and the retry_backlog columns (stage, attempts, next_retry_at, exhausted; None
        without a backlog row). Excluded unless `ignore_backlog`: backlog rows not yet
        due or exhausted. Always excluded: SKILLS_FAILED whose state_error says
        retryable=false (a stable failure)."""

    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        """id, permit_number, status, processing_state, state_error, issued_at."""

    def permit_state(self, permit_id: int) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(processing_state, state_error)."""

    def advance_state(self, permit_id: int, from_state: str, to_state: str,
                      error: Optional[Dict[str, Any]] = None) -> bool:
        """Compare-and-set; False when the permit was not in `from_state`."""

    def backlog(self, permit_id: int) -> Optional[Dict[str, Any]]: ...

    def upsert_backlog(self, permit_id: int, row: Dict[str, Any]) -> None:
        """stage, attempts, next_retry_at, exhausted, last_error, last_run_id."""

    def clear_backlog(self, permit_id: int) -> None: ...

    # ---- review ------------------------------------------------------------

    def review_queue(self, jurisdiction: str, *, states: Optional[Sequence[str]],
                     review_statuses: Optional[Sequence[str]], confidence: Optional[str],
                     limit: int) -> List[Dict[str, Any]]:
        """Permits joined to their latest inference: permit_id, permit_number, status,
        processing_state, inference_id, overall_confidence, skills, dropped_skills,
        assumptions, review_status, proposed_by, proposed_decision, review_note,
        reviewer, created_at. Oldest inference first."""

    def latest_inference(self, permit_id: int) -> Optional[Dict[str, Any]]:
        """The newest skill_inferences row (id, review_status, reviewer, reviewed_at,
        review_note, proposed_by, proposed_decision, input_snapshot_id, ...)."""

    def snapshot_summary(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        """id, final_url, fetch_strategy, fetched_at, content_hash, degraded."""

    def apply_review(self, *, inference_id: int, permit_id: int, review_status: str,
                     reviewer: Optional[str], note: Optional[str],
                     proposal: Optional[Tuple[str, str]],
                     transition: Optional[Tuple[str, str]]) -> bool:
        """In one transaction: update the inference's review columns (reviewed_at = now
        when `reviewer` is set; proposed_by/proposed_decision = `proposal` when given)
        and, with `transition` (from, to), compare-and-set the permit's state. Returns
        False, changing nothing, when the CAS misses."""

    # ---- dashboard ---------------------------------------------------------

    def cohort_status(self, jurisdiction: str, since: datetime) -> Dict[str, Any]:
        """Permits whose state changed since `since`: counts by processing_state,
        failure reasons (state_error.error) by state, and p50/p95 step latency from
        the step.* events since then."""

"""The permit lifecycle state machine (`permits.processing_state`), as one legal-edge table.

Three writers move a permit, each with its own compare-and-set:

- Stage 05 detail extraction (`mark_detail_state`): SYNCED/DETAIL_FAILED -> DETAIL_READY
  on success, SYNCED -> DETAIL_FAILED on failure.
- Stage 06 skill inference (`mark_skills_state`): DETAIL_READY/SKILLS_FAILED ->
  SKILLS_INFERRED (also SYNCED/DETAIL_FAILED in degraded mode), and the same
  sources -> SKILLS_FAILED on failure.
- Stage 07 (this package, `advance()`): non-retryable detail failure ->
  SKILLS_SKIPPED, not eligible for inference -> SKILLS_SKIPPED, human review
  -> SKILLS_APPROVED / SKILLS_REJECTED.

`EDGES` is the union. Orchestrator writes are checked before they are made
(`check` raises `IllegalTransition`); Stage 05/06 writes are observed after
each step and checked the same way (an illegal one is recorded as an
`ILLEGAL_TRANSITION` event). Same-state writes (a re-infer that keeps
SKILLS_INFERRED, a repeated failure) are not transitions.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Tuple

SYNCED = "SYNCED"
DETAIL_READY = "DETAIL_READY"
DETAIL_FAILED = "DETAIL_FAILED"
SKILLS_INFERRED = "SKILLS_INFERRED"
SKILLS_FAILED = "SKILLS_FAILED"
SKILLS_SKIPPED = "SKILLS_SKIPPED"
SKILLS_APPROVED = "SKILLS_APPROVED"
SKILLS_REJECTED = "SKILLS_REJECTED"

STATES: Tuple[str, ...] = (SYNCED, DETAIL_READY, SKILLS_INFERRED, SKILLS_APPROVED, SKILLS_REJECTED,
                           DETAIL_FAILED, SKILLS_FAILED, SKILLS_SKIPPED)
TERMINAL: FrozenSet[str] = frozenset({SKILLS_APPROVED, SKILLS_REJECTED, SKILLS_SKIPPED})

# (from, to) -> (owner, event)
EDGES: Dict[Tuple[str, str], Tuple[str, str]] = {
    # Stage 05 detail extraction
    (SYNCED, DETAIL_READY): ("detail", "detail_ok"),
    (DETAIL_FAILED, DETAIL_READY): ("detail", "detail_ok"),
    (SYNCED, DETAIL_FAILED): ("detail", "detail_failed"),
    # Stage 06 skill inference
    (DETAIL_READY, SKILLS_INFERRED): ("skills", "infer_ok"),
    (SKILLS_FAILED, SKILLS_INFERRED): ("skills", "infer_ok"),
    (SYNCED, SKILLS_INFERRED): ("skills", "infer_ok_degraded"),
    (DETAIL_FAILED, SKILLS_INFERRED): ("skills", "infer_ok_degraded"),
    (DETAIL_READY, SKILLS_FAILED): ("skills", "infer_failed"),
    (SYNCED, SKILLS_FAILED): ("skills", "infer_failed"),
    (DETAIL_FAILED, SKILLS_FAILED): ("skills", "infer_failed"),
    # Stage 07 orchestration
    (DETAIL_FAILED, SKILLS_SKIPPED): ("orchestration", "detail_failed_permanently"),
    (DETAIL_READY, SKILLS_SKIPPED): ("orchestration", "not_eligible"),
    (SKILLS_INFERRED, SKILLS_APPROVED): ("review", "human_approve"),
    (SKILLS_INFERRED, SKILLS_REJECTED): ("review", "human_reject"),
}

# The edges this package itself writes (via advance()).
ORCHESTRATOR_EDGES: FrozenSet[Tuple[str, str]] = frozenset(
    e for e, (owner, _) in EDGES.items() if owner in ("orchestration", "review"))


class IllegalTransition(ValueError):
    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"illegal transition {from_state} -> {to_state}")
        self.from_state, self.to_state = from_state, to_state


def is_legal(from_state: str, to_state: str) -> bool:
    """True for a legal edge; a same-state write is not a transition (also True)."""
    if from_state not in STATES or to_state not in STATES:
        return False
    return from_state == to_state or (from_state, to_state) in EDGES


def check(from_state: str, to_state: str, event: Optional[str] = None) -> Tuple[str, str]:
    """(owner, event) of a legal edge; raises IllegalTransition otherwise (same-state included,
    and when `event` is given but is not the edge's event)."""
    edge = EDGES.get((from_state, to_state))
    if edge is None or (event is not None and event != edge[1]):
        raise IllegalTransition(from_state, to_state)
    return edge

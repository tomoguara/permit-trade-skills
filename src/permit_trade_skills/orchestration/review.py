"""Human-in-the-loop review of skill inferences.

Two entry points, deliberately separate:

- `ReviewService.propose(...)`: the **agent** path (the planner's
  `submit_review` tool). Whatever the agent proposes (approve, reject,
  needs_human) is stored as `review_status = NEEDS_HUMAN` with
  `proposed_by = 'agent'` and the proposal in `proposed_decision`. It cannot
  carry a reviewer and never changes the permit's state.
- `ReviewService.decide(...)`: the **human** path (`pts review ...`). It needs
  a `HumanReviewer`, built only by `reviewer_from_env()` from `PTS_REVIEWER`.
  approve -> review_status APPROVED + permit SKILLS_INFERRED -> SKILLS_APPROVED;
  reject (note required) -> REJECTED + SKILLS_REJECTED; needs_human ->
  NEEDS_HUMAN (state unchanged). reviewer, reviewed_at and the note are stored.

Decisions apply to the permit's latest inference and only while it is
PENDING or NEEDS_HUMAN. Each one is an `orchestration_events` row
(`review.decision`, plus `transition` when the state moves).

Tests grep that only this module writes an approve/reject review_status and
that the planner/tools never call `decide(` or read `PTS_REVIEWER`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from permit_trade_skills.orchestration import states as S
from permit_trade_skills.orchestration.events import EventLog, new_run_id, utcnow
from permit_trade_skills.orchestration.ports import OrchestrationStore

PENDING, APPROVED, REJECTED, NEEDS_HUMAN = "PENDING", "APPROVED", "REJECTED", "NEEDS_HUMAN"
OPEN_STATUSES = (PENDING, NEEDS_HUMAN)
APPROVE, REJECT, NEEDS_HUMAN_DECISION = "approve", "reject", "needs_human"
DECISIONS = (APPROVE, REJECT, NEEDS_HUMAN_DECISION)
AGENT = "agent"
HUMAN = "human"
REVIEWER_ENV = "PTS_REVIEWER"

_HUMAN_OUTCOME = {
    APPROVE: (APPROVED, (S.SKILLS_INFERRED, S.SKILLS_APPROVED)),
    REJECT: (REJECTED, (S.SKILLS_INFERRED, S.SKILLS_REJECTED)),
    NEEDS_HUMAN_DECISION: (NEEDS_HUMAN, None),
}


class ReviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class HumanReviewer:
    name: str

    def __post_init__(self):
        name = (self.name or "").strip()
        if not name:
            raise ReviewError("REVIEWER_REQUIRED", f"set {REVIEWER_ENV} to the reviewer's name")
        if name.lower() == AGENT:
            raise ReviewError("REVIEWER_REQUIRED", f"{name!r} is not a human reviewer")


def reviewer_from_env(env: Optional[Mapping[str, str]] = None) -> HumanReviewer:
    """The human reviewer named by PTS_REVIEWER (raises REVIEWER_REQUIRED when unset)."""
    env = os.environ if env is None else env
    return HumanReviewer(env.get(REVIEWER_ENV, ""))


def _age_hours(created: Any, now: datetime) -> Optional[float]:
    if not isinstance(created, datetime):
        return None
    return round((now - created).total_seconds() / 3600, 1)


class ReviewService:
    def __init__(self, store: OrchestrationStore, skill_service: Any = None, *,
                 clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.skills = skill_service
        self.clock = clock

    # ---- reading -------------------------------------------------------------

    def queue(self, jurisdiction: str, *, state: Optional[str] = S.SKILLS_INFERRED,
              confidence: Optional[str] = None, needs_human: bool = False,
              limit: int = 50) -> List[Dict[str, Any]]:
        """`pts review list`: open inferences with their review summary."""
        rows = self.store.review_queue(jurisdiction, states=[state] if state else None,
                                       review_statuses=[NEEDS_HUMAN] if needs_human else list(OPEN_STATUSES),
                                       confidence=confidence, limit=limit)
        now = self.clock()
        return [{
            "permit_number": r["permit_number"], "status": r["status"], "processing_state": r["processing_state"],
            "inference_id": r["inference_id"], "overall_confidence": r.get("overall_confidence"),
            "skills": len(r.get("skills") or []), "dropped": len(r.get("dropped_skills") or []),
            "assumptions": len(r.get("assumptions") or []), "review_status": r["review_status"],
            "proposed_by": r.get("proposed_by"), "proposed_decision": r.get("proposed_decision"),
            "age_hours": _age_hours(r.get("created_at"), now),
        } for r in rows]

    def inspect(self, permit_number: str, jurisdiction: str) -> Dict[str, Any]:
        """`pts review inspect`: the inference, per-skill grounding, assumptions, detail provenance, review."""
        permit = self._permit(jurisdiction, permit_number)
        inf = self._latest(permit, permit_number)
        shown = self.skills.show(permit_number, jurisdiction) if self.skills is not None else None
        snapshot = (self.store.snapshot_summary(inf["input_snapshot_id"])
                    if inf.get("input_snapshot_id") is not None else None)
        return {"permit_number": permit_number, "jurisdiction": jurisdiction, "status": permit["status"],
                "processing_state": permit["processing_state"],
                "review": {k: inf.get(k) for k in ("review_status", "reviewer", "reviewed_at", "review_note",
                                                   "proposed_by", "proposed_decision")},
                "inference": shown["inference"] if shown else inf,
                "grounding": shown["grounding"] if shown else None,
                "evidence_source": shown["evidence_source"] if shown else None,
                "detail_snapshot": snapshot}

    # ---- agent path ------------------------------------------------------------

    def propose(self, permit_number: str, jurisdiction: str, decision: str, note: str, *,
                run_id: Optional[str] = None) -> Dict[str, Any]:
        """An agent's proposal: always stored as NEEDS_HUMAN with proposed_by='agent'."""
        decision = self._decision(decision)
        permit = self._permit(jurisdiction, permit_number)
        inf = self._open_inference(permit, permit_number)
        text = f"agent proposed {decision}" + (f": {note}" if note else "")
        if not self.store.apply_review(inference_id=inf["id"], permit_id=permit["id"], review_status=NEEDS_HUMAN,
                                       reviewer=None, note=text, proposal=(AGENT, decision), transition=None):
            raise ReviewError("CONFLICT", f"{permit_number}: the inference changed during the review")
        result = {"permit_number": permit_number, "inference_id": inf["id"], "review_status": NEEDS_HUMAN,
                  "proposed_by": AGENT, "proposed_decision": decision, "processing_state": permit["processing_state"]}
        EventLog(self.store, run_id or new_run_id("review")).record(
            "review.decision", permit_id=permit["id"], params={"decision": decision, "note": note, "by": AGENT},
            result=result)
        return result

    # ---- human path ------------------------------------------------------------

    def decide(self, permit_number: str, jurisdiction: str, decision: str, note: Optional[str], *,
               reviewer: HumanReviewer, run_id: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(reviewer, HumanReviewer):
            raise ReviewError("REVIEWER_REQUIRED", "a human reviewer (PTS_REVIEWER) is required")
        decision = self._decision(decision)
        note = (note or "").strip() or None
        if decision == REJECT and not note:
            raise ReviewError("NOTE_REQUIRED", "reject needs a note (--note)")
        permit = self._permit(jurisdiction, permit_number)
        inf = self._open_inference(permit, permit_number)
        review_status, transition = _HUMAN_OUTCOME[decision]
        if transition is not None:
            if permit["processing_state"] != transition[0]:
                raise ReviewError("INVALID_STATE", f"{permit_number} is {permit['processing_state']}; "
                                                   f"{decision} needs {transition[0]}")
            S.check(*transition)
        if not self.store.apply_review(inference_id=inf["id"], permit_id=permit["id"], review_status=review_status,
                                       reviewer=reviewer.name, note=note,
                                       proposal=(HUMAN, decision) if decision == NEEDS_HUMAN_DECISION else None,
                                       transition=transition):
            raise ReviewError("CONFLICT", f"{permit_number}: state changed during the review; reload and retry")
        new_state = transition[1] if transition else permit["processing_state"]
        result = {"permit_number": permit_number, "inference_id": inf["id"], "review_status": review_status,
                  "reviewer": reviewer.name, "note": note, "processing_state": new_state}
        events = EventLog(self.store, run_id or new_run_id("review"))
        events.record("review.decision", permit_id=permit["id"],
                      params={"decision": decision, "note": note, "by": HUMAN, "reviewer": reviewer.name},
                      result=result)
        if transition:
            events.record("transition", permit_id=permit["id"], from_state=transition[0], to_state=transition[1],
                          params={"owner": "review", "event": S.EDGES[transition][1]})
        return result

    def batch_candidates(self, jurisdiction: str, *, confidence: Optional[str],
                         limit: int = 500) -> List[Dict[str, Any]]:
        """Open SKILLS_INFERRED inferences at `confidence` (the bulk-approve preview list)."""
        return self.queue(jurisdiction, state=S.SKILLS_INFERRED, confidence=confidence, limit=limit)

    def decide_many(self, permit_numbers: Sequence[str], jurisdiction: str, decision: str,
                    note: Optional[str], *, reviewer: HumanReviewer) -> List[Dict[str, Any]]:
        run_id = new_run_id("review")
        out = []
        for number in permit_numbers:
            try:
                out.append(self.decide(number, jurisdiction, decision, note, reviewer=reviewer, run_id=run_id))
            except ReviewError as e:
                out.append({"permit_number": number, "error": e.code, "message": e.message})
        return out

    # ---- helpers ---------------------------------------------------------------

    @staticmethod
    def _decision(decision: str) -> str:
        d = (decision or "").strip().lower().replace("-", "_")
        if d not in DECISIONS:
            raise ReviewError("INVALID_DECISION", f"decision must be one of {', '.join(DECISIONS)}")
        return d

    def _permit(self, jurisdiction: str, permit_number: str) -> Dict[str, Any]:
        permit = self.store.get_permit(jurisdiction, permit_number)
        if permit is None:
            raise ReviewError("PERMIT_NOT_FOUND", f"permit {permit_number!r} not found in {jurisdiction!r}")
        return permit

    def _latest(self, permit: Dict[str, Any], permit_number: str) -> Dict[str, Any]:
        inf = self.store.latest_inference(permit["id"])
        if inf is None:
            raise ReviewError("NO_INFERENCE", f"{permit_number} has no skill inference to review")
        return inf

    def _open_inference(self, permit: Dict[str, Any], permit_number: str) -> Dict[str, Any]:
        inf = self._latest(permit, permit_number)
        if inf["review_status"] not in OPEN_STATUSES:
            raise ReviewError("ALREADY_REVIEWED", f"{permit_number}: latest inference is {inf['review_status']}")
        return inf

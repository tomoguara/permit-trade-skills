"""K6: persistence port + audit row for skill inference.

`skills/` never imports `permit_trade_skills.store` (Stage 04 boundary,
tests/unit/test_import_boundary.py). It talks to the `SkillStore` Protocol;
`permit_trade_skills.store.skills.SqlSkillStore` implements it and
`permit_trade_skills.wiring.skill_service()` wires it in.

Rows are append-only: a new prompt version or model adds a row and never
rewrites an old one; `latest_inference` picks the newest by created_at. The
same (permit, input_cache_key) is stored once (UNIQUE index), so a repeated
call returns the existing row.

`save_accepted` is the only writer of skill_inferences in `skills/`, and it
refuses an inference that did not pass `grounding.gate` (tests grep for this).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Protocol, Tuple

from permit_trade_skills.skills.grounding import GroundingReport
from permit_trade_skills.skills.schemas import InferenceFailed, SkillInference

log = logging.getLogger("pts.skills")

# State transitions owned by this stage (permits.processing_state)
SUCCESS_FROM = ("DETAIL_READY", "SKILLS_FAILED", "SKILLS_INFERRED", "SYNCED", "DETAIL_FAILED")
FAILURE_FROM = ("DETAIL_READY", "SKILLS_FAILED", "SYNCED", "DETAIL_FAILED")
INFERRED, FAILED = "SKILLS_INFERRED", "SKILLS_FAILED"


class SkillStore(Protocol):
    def get_permit(self, jurisdiction: str, permit_number: str) -> Optional[Dict[str, Any]]:
        """context.PERMIT_FIELDS for one permit."""

    def select_permits(self, jurisdiction: str, *, state: Optional[str], status: Optional[str],
                       limit: int) -> List[Dict[str, Any]]:
        """Batch cohort, newest issued first."""

    def latest_snapshot(self, permit_id: int) -> Optional[Dict[str, Any]]:
        """Latest detail_snapshots row (all columns)."""

    def snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]: ...

    def find_inference(self, permit_id: int, input_cache_key: str) -> Optional[Dict[str, Any]]: ...

    def latest_inference(self, permit_id: int) -> Optional[Dict[str, Any]]: ...

    def save_inference(self, row: Dict[str, Any]) -> Tuple[int, bool]:
        """INSERT ... ON CONFLICT (permit_id, input_cache_key) DO NOTHING. Returns (id, inserted)."""

    def mark_skills_state(self, permit_id: int, to_state: str, from_states: Tuple[str, ...],
                          error: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Compare-and-set processing_state; returns the new state or None when unchanged."""


def inference_row(permit_id: int, inf: SkillInference, *, substrate_text: Optional[str]) -> Dict[str, Any]:
    return {
        "permit_id": permit_id, "prompt_version": inf.prompt_version, "model": inf.model,
        "request_id": inf.request_id, "skills": inf.skills, "assumptions": inf.assumptions,
        "input_snapshot_id": inf.input_snapshot_id, "review_status": "PENDING",
        "prompt_hash": inf.prompt_hash, "completion_hash": inf.completion_hash, "context_hash": inf.context_hash,
        "input_cache_key": inf.input_cache_key, "degraded": inf.degraded, "substrate_hash": inf.substrate_hash,
        "substrate_text": substrate_text if inf.degraded else None,
        "overall_confidence": inf.overall_confidence, "licensing_notes": inf.licensing_notes,
        "dropped_skills": inf.dropped_skills, "warnings": inf.warnings, "signal_check": inf.signal_check,
    }


def from_row(row: Dict[str, Any], *, permit_number: str, jurisdiction: str, cached: bool = True) -> SkillInference:
    skills = list(row.get("skills") or [])
    created = row.get("created_at")
    return SkillInference(
        permit_number=permit_number, jurisdiction=jurisdiction, model=row["model"],
        prompt_version=row["prompt_version"], skills=skills, assumptions=list(row.get("assumptions") or []),
        overall_confidence=row.get("overall_confidence") or "low",
        evidence_source=(skills[0].get("evidence_source") if skills else "") or "",
        degraded=bool(row.get("degraded")), licensing_notes=list(row.get("licensing_notes") or []),
        dropped_skills=list(row.get("dropped_skills") or []), warnings=list(row.get("warnings") or []),
        signal_check=row.get("signal_check"), request_id=row.get("request_id"),
        prompt_hash=row.get("prompt_hash"), completion_hash=row.get("completion_hash"),
        context_hash=row.get("context_hash"), input_cache_key=row.get("input_cache_key"),
        input_snapshot_id=row.get("input_snapshot_id"), substrate_hash=row.get("substrate_hash"),
        review_status=row.get("review_status") or "PENDING",
        generated_at=created.isoformat() + "Z" if hasattr(created, "isoformat") else created,
        inference_id=row.get("id"), cached=cached,
    )


def save_accepted(store: SkillStore, permit_id: int, inf: SkillInference, grounding: GroundingReport, *,
                  substrate_text: Optional[str]) -> SkillInference:
    """Insert the accepted row and advance the permit to SKILLS_INFERRED."""
    if not grounding.passed or len(inf.skills) != len(grounding.accepted):
        raise ValueError("refusing to persist an inference that did not pass the grounding gate")
    inference_id, inserted = store.save_inference(inference_row(permit_id, inf, substrate_text=substrate_text))
    if not inserted:   # a concurrent worker stored the same inputs first: return its row
        existing = store.find_inference(permit_id, inf.input_cache_key or "")
        if existing is not None:
            inf = from_row(existing, permit_number=inf.permit_number, jurisdiction=inf.jurisdiction)
    inf.inference_id = inference_id
    store.mark_skills_state(permit_id, INFERRED, SUCCESS_FROM)
    log.info(json.dumps({"event": "skills.accepted", "permit": inf.permit_key, "inference_id": inference_id,
                         "inserted": inserted, "skills": len(inf.skills), "dropped": len(inf.dropped_skills),
                         "overall_confidence": inf.overall_confidence, "degraded": inf.degraded,
                         "model": inf.model, "request_id": inf.request_id}))
    return inf


def mark_failed(store: SkillStore, permit_id: int, error: InferenceFailed) -> Optional[str]:
    """SKILLS_FAILED with state_error; a permit with an accepted inference keeps SKILLS_INFERRED."""
    state = store.mark_skills_state(permit_id, FAILED, FAILURE_FROM, error.state_error())
    log.warning(json.dumps({"event": "skills.failed", "permit": error.permit_number, "state": state,
                            **error.state_error()}, default=str))
    return state

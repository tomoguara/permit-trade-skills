"""SkillService: loads a permit and its stored detail through the port, then calls `infer_skills`.

Used by `pts skills ...` and (Stage 07) the `infer_trade_skills` /
`get_skill_inference` tools. Skill inference never fetches pages: a permit
without a stored snapshot is NotEligible unless `allow_degraded` is set
(`pts detail fetch` / Stage 07 `fetch_detail` fetch it first).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from permit_trade_skills.detail.models import DetailDocument
from permit_trade_skills.skills import grounding as G
from permit_trade_skills.skills.context import PermitInput
from permit_trade_skills.skills.infer import infer_skills
from permit_trade_skills.skills.persist import SkillStore, from_row
from permit_trade_skills.skills.schemas import InferenceFailed, NotEligible, SkillInference

SUBSTRATE_UNAVAILABLE = "substrate_unavailable"


class PermitNotFound(LookupError):
    pass


def snapshot_document(snap: Dict[str, Any], permit_number: str, jurisdiction: str) -> DetailDocument:
    return DetailDocument(
        permit_number=permit_number, jurisdiction=jurisdiction, final_url=snap["final_url"],
        content_text=snap["content_text"], fields=snap.get("fields") or {},
        fetch_strategy=snap["fetch_strategy"], content_hash=snap["content_hash"],
        degraded=bool(snap.get("degraded")), notes=list(snap.get("notes") or []),
        signals=snap.get("signals"), fetched_at=snap.get("fetched_at"), snapshot_id=snap.get("id"),
    )


class SkillService:
    def __init__(self, store: SkillStore, provider: Any = None, *,
                 provider_factory: Optional[Callable[[], Any]] = None,
                 settings: Optional[Dict[str, Any]] = None):
        self.store = store
        self._provider = provider
        self._provider_factory = provider_factory
        self.settings = settings

    @property
    def provider(self):
        if self._provider is None:
            if self._provider_factory is not None:
                self._provider = self._provider_factory()
            else:
                from permit_trade_skills.llm import get_provider

                self._provider = get_provider("skills.infer")
        return self._provider

    # ---- loading -------------------------------------------------------------

    def permit(self, jurisdiction: str, permit_number: str) -> PermitInput:
        row = self.store.get_permit(jurisdiction, permit_number)
        if row is None:
            raise PermitNotFound(f"permit {permit_number!r} not found in {jurisdiction!r}")
        return PermitInput.from_row(row)

    def detail(self, permit: PermitInput) -> Optional[DetailDocument]:
        snap = self.store.latest_snapshot(permit.permit_id)
        return snapshot_document(snap, permit.permit_number, permit.jurisdiction) if snap else None

    # ---- inference -------------------------------------------------------------

    def infer(self, permit_number: str, jurisdiction: str, *, allow_degraded: bool = False,
              allowed_statuses: Optional[Sequence[str]] = None) -> SkillInference:
        permit = self.permit(jurisdiction, permit_number)
        return infer_skills(permit, self.detail(permit), provider=self.provider, store=self.store,
                            allowed_statuses=allowed_statuses, allow_degraded=allow_degraded,
                            settings=self.settings)

    def batch(self, jurisdiction: str, *, state: Optional[str] = "DETAIL_READY", status: Optional[str] = None,
              limit: int = 25, allow_degraded: bool = False,
              allowed_statuses: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Infer a cohort sequentially. One permit's failure never stops the batch."""
        rows = self.store.select_permits(jurisdiction, state=state, status=status, limit=limit)
        results: List[Dict[str, Any]] = []
        for row in rows:
            permit = PermitInput.from_row(row)
            started = time.monotonic()
            entry: Dict[str, Any] = {"permit_number": permit.permit_number}
            try:
                inf = infer_skills(permit, self.detail(permit), provider=self.provider, store=self.store,
                                   allowed_statuses=allowed_statuses, allow_degraded=allow_degraded,
                                   settings=self.settings)
                entry.update(outcome="cached" if inf.cached else "inferred", inference_id=inf.inference_id,
                             skills=len(inf.skills), dropped=len(inf.dropped_skills),
                             overall_confidence=inf.overall_confidence, degraded=inf.degraded)
            except NotEligible as e:
                entry.update(outcome="not_eligible", reason=e.reason)
            except InferenceFailed as e:
                entry.update(outcome="failed", error=e.code, message=e.message, retryable=e.retryable)
            entry["duration_ms"] = int((time.monotonic() - started) * 1000)
            results.append(entry)
        counts = {k: sum(1 for r in results if r["outcome"] == k)
                  for k in ("inferred", "cached", "failed", "not_eligible")}
        return {"jurisdiction": jurisdiction, "state": state, "status": status, "total": len(results),
                **counts, "results": results}

    # ---- reading -------------------------------------------------------------

    def latest(self, permit_number: str, jurisdiction: str) -> Optional[SkillInference]:
        """The newest stored inference (no LLM call): Stage 07 `get_skill_inference`."""
        permit = self.permit(jurisdiction, permit_number)
        row = self.store.latest_inference(permit.permit_id)
        return from_row(row, permit_number=permit_number, jurisdiction=jurisdiction, cached=False) if row else None

    def show(self, permit_number: str, jurisdiction: str) -> Optional[Dict[str, Any]]:
        """The latest inference with each skill re-checked against its stored substrate (✓/✗)."""
        permit = self.permit(jurisdiction, permit_number)
        row = self.store.latest_inference(permit.permit_id)
        if row is None:
            return None
        inf = from_row(row, permit_number=permit_number, jurisdiction=jurisdiction, cached=False)
        substrate, source_url = None, None
        if row.get("degraded"):
            substrate, source_url = row.get("substrate_text"), permit.detail_url
        elif row.get("input_snapshot_id") is not None:
            snap = self.store.snapshot(row["input_snapshot_id"])
            if snap is not None:
                substrate, source_url = snap["content_text"], snap["final_url"]
        if substrate is None:
            table = [{"skill": s.get("skill"), "trade": s.get("trade"), "evidence": s.get("evidence"),
                      "confidence": s.get("confidence"), "reasons": [SUBSTRATE_UNAVAILABLE]} for s in inf.skills]
        else:
            table = G.verify_row(inf.skills, substrate, source_url or "")
        for entry in table:
            entry["grounded"] = not entry["reasons"]
        return {"inference": inf.to_dict(), "contract": inf.to_contract(), "grounding": table,
                "evidence_source": source_url, "processing_state": permit.processing_state}

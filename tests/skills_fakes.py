"""Shared helpers for the Stage 06 skill tests: fixtures, an in-memory SkillStore, a context-keyed fake LLM.

Fixtures (tests/fixtures/skills/*.json): `permit` (catalog row), `detail`
(DetailDocument fields, or null), `response` (the canned model output) and
`expected` (outcome, trades present/absent, overall confidence bound).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from permit_trade_skills.detail.models import DetailDocument
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.skills.context import PermitInput, build_context, render

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills"
POSITIVE = ("basement", "addition", "tenant_improvement", "demolition", "no_scope")
NEGATIVE = ("injection", "wrong_permit", "admin_only")
SETTINGS = {"temperature": 0.1, "max_tokens": 4000, "max_context_chars": 8000, "allowed_statuses": ["ISSUED"]}


def load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def permit_input(fx: Dict[str, Any], **overrides) -> PermitInput:
    row = {**fx["permit"], **overrides}
    if isinstance(row.get("issued_at"), str):
        row["issued_at"] = datetime.fromisoformat(row["issued_at"])
    return PermitInput.from_row(row)


def detail_document(fx: Dict[str, Any], **overrides) -> Optional[DetailDocument]:
    if fx.get("detail") is None:
        return None
    d = {**fx["detail"], **overrides}
    return DetailDocument(permit_number=d["permit_number"], jurisdiction=d["jurisdiction"],
                          final_url=d["final_url"], content_text=d["content_text"], fields=d.get("fields") or {},
                          fetch_strategy=d["fetch_strategy"], content_hash=d["content_hash"],
                          degraded=bool(d.get("degraded")), notes=list(d.get("notes") or []),
                          snapshot_id=d.get("snapshot_id"))


def fixture_provider(*names: str, responses: Optional[Dict[str, Any]] = None,
                     allow_degraded: bool = False) -> FakeProvider:
    """A FakeProvider answering each fixture's canned response, keyed by its context_hash.

    The user prompt is a pure function of the context, so each rendered prompt
    maps to one context_hash and from there to the fixture's response. The
    corrective-retry prompt (the original plus an appended error) resolves to
    the same context.
    """
    by_prompt: Dict[str, str] = {}
    by_hash: Dict[str, Any] = {}
    for name in names:
        fx = load(name)
        ctx = build_context(permit_input(fx), detail_document(fx), max_chars=SETTINGS["max_context_chars"],
                            allow_degraded=allow_degraded)
        by_prompt[render(ctx)[1]] = ctx.context_hash
        by_hash[ctx.context_hash] = copy.deepcopy((responses or {}).get(name, fx["response"]))

    def respond(system: str, user: str):
        for prompt, context_hash in by_prompt.items():
            if user.startswith(prompt):
                return copy.deepcopy(by_hash[context_hash])
        raise KeyError(f"no fixture for prompt {user[:80]!r}")

    return FakeProvider(respond, model="fake-skills")


class SequenceProvider(FakeProvider):
    """Answers call i with responses[i] (dicts or exceptions)."""

    def __init__(self, responses: List[Any]):
        self._queue = list(responses)
        super().__init__(lambda system, user: self._queue.pop(0), model="fake-skills")


class MemorySkillStore:
    """In-memory SkillStore (skills.persist port) for offline tests."""

    def __init__(self, permits: Optional[List[Dict[str, Any]]] = None):
        self.permits: Dict[int, Dict[str, Any]] = {p["id"]: dict(p) for p in (permits or [])}
        self.snapshots: Dict[int, Dict[str, Any]] = {}
        self.inferences: List[Dict[str, Any]] = []
        self.state_errors: Dict[int, Any] = {}

    def add_fixture(self, fx: Dict[str, Any], *, with_snapshot: bool = True) -> None:
        """Register the fixture's permit (kept if already present) and its snapshot."""
        if fx["permit"]["id"] not in self.permits:
            row = dict(fx["permit"])
            if isinstance(row.get("issued_at"), str):
                row["issued_at"] = datetime.fromisoformat(row["issued_at"])
            self.permits[row["id"]] = row
        if with_snapshot and fx.get("detail"):
            d = fx["detail"]
            self.snapshots[d["snapshot_id"]] = {"id": d["snapshot_id"], "permit_id": fx["permit"]["id"],
                                                "fetched_at": datetime(2026, 11, 6), **d}

    def get_permit(self, jurisdiction, permit_number):
        for p in self.permits.values():
            if p["jurisdiction_id"] == jurisdiction and p["permit_number"] == permit_number:
                return dict(p)
        return None

    def select_permits(self, jurisdiction, *, state=None, status=None, limit=25):
        rows = [p for p in self.permits.values() if p["jurisdiction_id"] == jurisdiction
                and (not state or p.get("processing_state") == state) and (not status or p.get("status") == status)]
        return [dict(p) for p in rows[:limit]]

    def latest_snapshot(self, permit_id):
        snaps = [s for s in self.snapshots.values() if s["permit_id"] == permit_id]
        return dict(max(snaps, key=lambda s: s["id"])) if snaps else None

    def snapshot(self, snapshot_id):
        s = self.snapshots.get(snapshot_id)
        return dict(s) if s else None

    def find_inference(self, permit_id, input_cache_key):
        for row in self.inferences:
            if row["permit_id"] == permit_id and row["input_cache_key"] == input_cache_key:
                return dict(row)
        return None

    def latest_inference(self, permit_id):
        rows = [r for r in self.inferences if r["permit_id"] == permit_id]
        return dict(rows[-1]) if rows else None

    def save_inference(self, row) -> Tuple[int, bool]:
        existing = self.find_inference(row["permit_id"], row["input_cache_key"])
        if existing:
            return existing["id"], False
        stored = {**row, "id": len(self.inferences) + 1, "created_at": datetime(2026, 11, 6, 12, 0)}
        self.inferences.append(stored)
        return stored["id"], True

    def mark_skills_state(self, permit_id, to_state, from_states, error=None):
        p = self.permits[permit_id]
        if p.get("processing_state") not in from_states:
            return None
        p["processing_state"] = to_state
        self.state_errors[permit_id] = error
        return to_state

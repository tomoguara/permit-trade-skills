"""Offline environment for the Stage 07 planner, review and replay tests (no database, no network).

`agent_env()` seeds an in-memory store with the Stage 06 skill fixtures and
returns all nine `toolset-v1` tools over it:

- the four Stage 04 query tools: `MemoryQuery.call`, a small re-implementation
  of the filters the planner uses (status, class, type, cost, issued dates,
  sort, page size; class/type match exactly, case-insensitively, as Stage 04);
- ensure_detail / get_detail: `FakeDetailService` over the stored snapshots
  (`fail` scripts DetailUnavailable per permit);
- infer_trade_skills / get_skill_inference: the real Stage 06 `SkillService`
  on `MemorySkillStore`, with a FakeProvider answering each permit's fixture
  response (`env.skill_provider.calls` counts the expensive LLM calls);
- submit_review: the real `ReviewService` on `MemoryOrchestrationStore`.

`ScriptedPlanner` is the planner model: it answers turn i with `script[i]`
(an action dict, or a callable(user_prompt) -> action); past the end it
repeats the last entry.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from permit_trade_skills.detail import DetailUnavailable
from permit_trade_skills.detail.pipeline import snapshot_document
from permit_trade_skills.llm.fake import FakeProvider, by_substring
from permit_trade_skills.orchestration.review import ReviewService
from permit_trade_skills.orchestration.tools import Toolset, build_toolset_v1
from permit_trade_skills.skills.service import SkillService

from skills_fakes import SETTINGS as SKILL_SETTINGS
from skills_fakes import MemorySkillStore, load

JURISDICTION = "seattle-kingcounty-wa"
ROW_KEYS = ("permit_number", "status", "permit_class", "permit_class_mapped", "permit_type_mapped",
            "permit_type_desc", "description", "valuation", "issued_at", "address", "detail_url",
            "processing_state")


# ---- persistence -----------------------------------------------------------------

class MemoryOrchestrationStore:
    """The orchestration port over a MemorySkillStore (permits, snapshots, inferences)."""

    def __init__(self, skills: MemorySkillStore):
        self.skills = skills
        self.events: List[Dict[str, Any]] = []

    def add_event(self, event):
        self.events.append({**copy.deepcopy(event), "id": len(self.events) + 1})

    def run_events(self, run_id):
        return [copy.deepcopy(e) for e in self.events if e["run_id"] == run_id]

    def active_jurisdictions(self):
        return [JURISDICTION]

    def get_permit(self, jurisdiction, permit_number):
        p = self.skills.get_permit(jurisdiction, permit_number)
        if p is None:
            return None
        return {"id": p["id"], "permit_number": p["permit_number"], "status": p["status"],
                "processing_state": p["processing_state"], "state_error": self.skills.state_errors.get(p["id"]),
                "issued_at": p.get("issued_at")}

    def permit_state(self, permit_id):
        p = self.skills.permits[permit_id]
        return p["processing_state"], self.skills.state_errors.get(permit_id)

    def advance_state(self, permit_id, from_state, to_state, error=None):
        p = self.skills.permits[permit_id]
        if p["processing_state"] != from_state:
            return False
        p["processing_state"] = to_state
        self.skills.state_errors[permit_id] = error
        return True

    def latest_inference(self, permit_id):
        row = self.skills.latest_inference(permit_id)
        if row is None:
            return None
        return {"review_status": "PENDING", "reviewer": None, "reviewed_at": None, "review_note": None,
                "proposed_by": None, "proposed_decision": None, **row}

    def _row(self, inference_id):
        return next(r for r in self.skills.inferences if r["id"] == inference_id)

    def snapshot_summary(self, snapshot_id):
        s = self.skills.snapshot(snapshot_id)
        return {k: s.get(k) for k in ("id", "final_url", "fetch_strategy", "fetched_at", "content_hash",
                                      "degraded")} if s else None

    def apply_review(self, *, inference_id, permit_id, review_status, reviewer, note, proposal, transition):
        row = self._row(inference_id)
        if row.get("review_status", "PENDING") not in ("PENDING", "NEEDS_HUMAN"):
            return False
        if transition is not None and not self.advance_state(permit_id, *transition):
            return False
        row.update(review_status=review_status, reviewer=reviewer, review_note=note,
                   reviewed_at=datetime(2026, 11, 10, 12) if reviewer else None)
        if proposal is not None:
            row["proposed_by"], row["proposed_decision"] = proposal
        return True

    def review_queue(self, jurisdiction, *, states, review_statuses, confidence, limit):
        out = []
        for p in self.skills.permits.values():
            inf = self.latest_inference(p["id"])
            if inf is None or (states and p["processing_state"] not in states) \
                    or (review_statuses and inf["review_status"] not in review_statuses) \
                    or (confidence and inf.get("overall_confidence") != confidence):
                continue
            out.append({"permit_id": p["id"], "permit_number": p["permit_number"], "status": p["status"],
                        "processing_state": p["processing_state"], "inference_id": inf["id"],
                        **{k: inf.get(k) for k in ("overall_confidence", "skills", "dropped_skills", "assumptions",
                                                   "review_status", "proposed_by", "proposed_decision",
                                                   "review_note", "reviewer", "created_at")}})
        return out[:limit]


# ---- services ------------------------------------------------------------------------

class MemoryQuery:
    """Stage 04 query tools over the memory store (`call(name, args)` -> {ok, data} | error)."""

    def __init__(self, store: MemorySkillStore):
        self.store = store
        self.calls: List[Dict[str, Any]] = []

    @staticmethod
    def _row(p: Dict[str, Any]) -> Dict[str, Any]:
        row = {k: p.get(k) for k in ROW_KEYS}
        if isinstance(row["issued_at"], datetime):
            row["issued_at"] = row["issued_at"].isoformat() + "Z"
        return row

    @staticmethod
    def _matches(p: Dict[str, Any], a: Dict[str, Any]) -> bool:
        def one_of(value, wanted):
            wanted = [wanted] if isinstance(wanted, str) else wanted
            return value is not None and value.lower() in [w.lower() for w in wanted]

        issued = p.get("issued_at")
        return all([
            not a.get("status") or p["status"] in a["status"],
            not a.get("permit_class") or one_of(p.get("permit_class"), a["permit_class"])
            or one_of(p.get("permit_class_mapped"), a["permit_class"]),
            not a.get("permit_type") or one_of(p.get("permit_type_mapped"), a["permit_type"])
            or one_of(p.get("permit_type_desc"), a["permit_type"]),
            a.get("min_cost") is None or (p.get("valuation") or 0) >= a["min_cost"],
            a.get("max_cost") is None or (p.get("valuation") or 0) <= a["max_cost"],
            not a.get("issued_after") or (issued and issued >= datetime.fromisoformat(a["issued_after"])),
            not a.get("issued_before") or (issued and issued < datetime.fromisoformat(a["issued_before"])),
        ])

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"tool": name, "args": copy.deepcopy(args)})
        permits = list(self.store.permits.values())
        if name == "list_jurisdictions":
            return {"ok": True, "data": {"jurisdictions": [{"slug": JURISDICTION, "record_count": len(permits)}],
                                         "count": 1}}
        if name == "search_permits":
            rows = [p for p in permits if self._matches(p, args)]
            if args.get("sort") == "cost_desc":
                rows.sort(key=lambda p: -(p.get("valuation") or 0))
            else:
                rows.sort(key=lambda p: p.get("issued_at") or datetime.min, reverse=True)
            rows = rows[: args.get("page_size", 25)]
            return {"ok": True, "data": {"jurisdiction": JURISDICTION, "query": args, "count": len(rows),
                                         "next_cursor": None, "rows": [self._row(p) for p in rows]}}
        if name == "get_permit":
            p = self.store.get_permit(JURISDICTION, args["permit_number"])
            if p is None:
                return {"ok": False, "error_code": "PERMIT_NOT_FOUND", "retryable": False,
                        "message": f"Permit {args['permit_number']!r} not found."}
            snap = self.store.latest_snapshot(p["id"])
            inf = self.store.latest_inference(p["id"])
            return {"ok": True, "data": {
                "jurisdiction": JURISDICTION, "permit": self._row(p), "status_history": [],
                "detail_snapshot": {"present": True, "final_url": snap["final_url"]} if snap else {"present": False},
                "skill_inference": ({"id": inf["id"], "review_status": inf.get("review_status", "PENDING"),
                                     "skills": inf["skills"], "assumptions": inf["assumptions"]} if inf else None)}}
        if name == "permit_status_counts":
            counts: Dict[str, int] = {}
            for p in permits:
                counts[p["status"]] = counts.get(p["status"], 0) + 1
            return {"ok": True, "data": {"jurisdiction": JURISDICTION, "counts": counts, "total": len(permits)}}
        return {"ok": False, "error_code": "INVALID_PARAMETER", "retryable": False, "message": f"unknown {name}"}


class FakeDetailService:
    """Stage 05 DetailService stand-in over the memory snapshots."""

    def __init__(self, store: MemorySkillStore, fail: Optional[Dict[str, DetailUnavailable]] = None):
        self.store = store
        self.fail = dict(fail or {})
        self.ensure_calls: List[str] = []

    def latest(self, permit_number, jurisdiction):
        p = self.store.get_permit(jurisdiction, permit_number)
        if p is None:
            return None
        snap = self.store.latest_snapshot(p["id"])
        return snapshot_document(snap, permit_number, jurisdiction) if snap else None

    def ensure_fresh(self, permit_number, jurisdiction, max_age=None, **kw):
        self.ensure_calls.append(permit_number)
        if permit_number in self.fail:
            raise self.fail[permit_number]
        if self.store.get_permit(jurisdiction, permit_number) is None:
            raise LookupError(f"permit {permit_number!r} not found in {jurisdiction!r}")
        doc = self.latest(permit_number, jurisdiction)
        if doc is None:
            raise DetailUnavailable(permit_number, "NOT_FOUND", False)
        return doc


# ---- the scripted planner model --------------------------------------------------------

Action = Union[Dict[str, Any], Callable[[str], Dict[str, Any]]]


def tool(name: str, thought: str = "next step", **args: Any) -> Dict[str, Any]:
    return {"thought": thought, "action": "tool_call", "tool": name, "arguments_json": json.dumps(args),
            "final_answer": None}


def final(findings: Optional[List[Dict[str, Any]]], *, unfulfilled: Sequence[Dict[str, Any]] = (),
          confidence: str = "high", citations: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    return {"thought": "done", "action": "final_answer", "tool": None, "arguments_json": None,
            "final_answer": {"findings": findings, "unfulfilled": list(unfulfilled), "confidence": confidence,
                             "citations": list(citations)}}


class ScriptedPlanner(FakeProvider):
    def __init__(self, script: Sequence[Action], *, clock: Any = None, step_s: float = 0.0):
        self.script = list(script)
        self.turn = 0
        self.clock, self.step_s = clock, step_s
        super().__init__(self._next, model="fake-planner")

    def _next(self, system: str, user: str):
        entry = self.script[min(self.turn, len(self.script) - 1)]
        self.turn += 1
        if self.clock is not None:
            self.clock.advance(self.step_s)
        return entry(user) if callable(entry) else copy.deepcopy(entry)


class Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


# ---- the environment ---------------------------------------------------------------------

FIXTURES = ("basement", "addition", "demolition", "tenant_improvement")


@dataclass
class AgentEnv:
    store: MemorySkillStore
    orchestration: MemoryOrchestrationStore
    query: MemoryQuery
    detail: FakeDetailService
    skills: SkillService
    skill_provider: FakeProvider
    review: ReviewService
    toolset: Toolset

    def finding(self, permit_number: str) -> Dict[str, Any]:
        """The finding a faithful planner writes from the stored inference."""
        p = self.store.get_permit(JURISDICTION, permit_number)
        inf = self.store.latest_inference(p["id"])
        return {"permit_number": permit_number, "status": p["status"], "detail_present": True,
                "skills": [{k: s[k] for k in ("skill", "trade", "evidence", "confidence")} for s in inf["skills"]]}


def agent_env(*, overrides: Optional[Dict[str, Dict[str, Any]]] = None,
              detail_fail: Optional[Dict[str, DetailUnavailable]] = None) -> AgentEnv:
    """Seed the four positive Stage 06 fixtures. The basement permit (7083363-CN) is re-valued
    at $125k by default so that two residential additions over $100k exist (AC1)."""
    overrides = {"basement": {"valuation": 125000}, **(overrides or {})}
    store = MemorySkillStore()
    responses = {}
    for name in FIXTURES:
        fx = load(name)
        fx["permit"].update(overrides.get(name, {}))
        store.add_fixture(fx)
        responses[f"PERMIT # {fx['permit']['permit_number']}"] = fx["response"]
    orchestration = MemoryOrchestrationStore(store)
    provider = FakeProvider(by_substring(responses), model="fake-skills")
    skills = SkillService(store, provider, settings=SKILL_SETTINGS)
    review = ReviewService(orchestration, skills)
    query = MemoryQuery(store)
    detail = FakeDetailService(store, detail_fail)
    toolset = build_toolset_v1(query_call=query.call, detail_service=detail, skill_service=skills,
                               review_service=review)
    return AgentEnv(store, orchestration, query, detail, skills, provider, review, toolset)

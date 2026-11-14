"""The planner's toolset (`toolset-v1`, closed) and the guarded executor.

`AgentTool` is the only seam between the planner and the system's
capabilities. The nine v1 tools:

| tool                   | wraps                                   | cost class     | idempotent |
|------------------------|-----------------------------------------|----------------|------------|
| search_permits         | Stage 04 query tool                     | free           | yes        |
| get_permit             | Stage 04 query tool                     | free           | yes        |
| permit_status_counts   | Stage 04 query tool                     | free           | yes        |
| list_jurisdictions     | Stage 04 query tool                     | free           | yes        |
| ensure_detail          | Stage 05 DetailService.ensure_fresh     | cheap          | yes        |
| get_detail             | Stage 05 DetailService.latest           | free           | yes        |
| infer_trade_skills     | Stage 06 SkillService.infer             | expensive_llm  | yes (cache)|
| get_skill_inference    | Stage 06 SkillService.latest            | free           | yes        |
| submit_review          | ReviewService.propose (agent path)      | free           | no         |

Every tool returns a `ToolResult` `{ok, data | error_code, message, retryable}`
and never raises. The planner writes nothing directly: its only writes are
through these Stage 05/06 services and the review proposal.

`ToolExecutor.call` enforces, in code: known tool; arguments valid against
the tool's JSON Schema (else `INVALID_PARAMETER`, nothing runs); the call
budgets; no repeat of a successful non-idempotent call (`DUPLICATE_CALL`);
one automatic retry of a retryable failure; a branch (tool + arguments) that
failed twice is abandoned (`BRANCH_ABANDONED`). Every call, refused or run,
is a `tool.call` event with its params and result.

Changing a tool's name, parameters or result shape is a new toolset version
(see docs/ops.md, "Adding a tool").
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from permit_trade_skills.detail import DetailUnavailable
from permit_trade_skills.llm import ToolSpec
from permit_trade_skills.orchestration.budgets import EXPENSIVE, Budget
from permit_trade_skills.orchestration.events import EventLog, current_run_id
from permit_trade_skills.orchestration.review import DECISIONS, ReviewError
from permit_trade_skills.query.tools import TOOL_SCHEMAS
from permit_trade_skills.skills import InferenceFailed, NotEligible

TOOLSET_VERSION = "toolset-v1"
FREE, CHEAP = "free", "cheap"

# Error codes produced here (tools add the wrapped stage's own codes).
INVALID_PARAMETER = "INVALID_PARAMETER"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
DUPLICATE_CALL = "DUPLICATE_CALL"
BRANCH_ABANDONED = "BRANCH_ABANDONED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
PERMIT_NOT_FOUND = "PERMIT_NOT_FOUND"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
NO_DETAIL = "NO_DETAIL"
NO_INFERENCE = "NO_INFERENCE"
INTERNAL_ERROR = "INTERNAL_ERROR"
REFUSALS = (INVALID_PARAMETER, UNKNOWN_TOOL, DUPLICATE_CALL, BRANCH_ABANDONED, BUDGET_EXHAUSTED)


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    retryable: bool = False

    @classmethod
    def success(cls, data: Any) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def error(cls, code: str, message: str, retryable: bool = False) -> "ToolResult":
        return cls(ok=False, error_code=code, message=message, retryable=retryable)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ToolResult":
        if d.get("ok"):
            return cls.success(d.get("data"))
        return cls.error(d.get("error_code") or INTERNAL_ERROR, d.get("message") or "", bool(d.get("retryable")))

    def to_dict(self) -> Dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error_code": self.error_code, "message": self.message, "retryable": self.retryable}


@dataclass
class AgentTool:
    name: str
    description: str                       # LLM-facing
    params: Dict[str, Any]                 # JSON Schema
    fn: Callable[[Dict[str, Any]], ToolResult]
    idempotent: bool = True
    cost_class: str = FREE                 # free | cheap | expensive_llm
    read_only: bool = True                 # replay may re-run it as is
    replay_as: Optional[str] = None        # read-only stand-in for replay

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.params)


class Toolset:
    def __init__(self, tools: List[AgentTool], version: str = TOOLSET_VERSION):
        self.version = version
        self.tools: Dict[str, AgentTool] = {t.name: t for t in tools}

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(self.tools)

    def get(self, name: str) -> Optional[AgentTool]:
        return self.tools.get(name)

    def specs(self) -> List[ToolSpec]:
        return [t.spec() for t in self.tools.values()]

    def catalogue(self) -> str:
        """The tool list as the planner's system prompt shows it."""
        lines = []
        for t in self.tools.values():
            lines.append(f"- {t.name} [{t.cost_class}{'' if t.idempotent else ', not idempotent'}]: "
                         f"{t.description}\n  parameters: {json.dumps(t.params, separators=(',', ':'))}")
        return "\n".join(lines)


# ---- the v1 tools ------------------------------------------------------------

_PERMIT_ARGS = {
    "type": "object",
    "properties": {
        "permit_number": {"type": "string", "minLength": 1, "description": "e.g. 7083363-CN"},
        "jurisdiction": {"type": "string", "description": "Jurisdiction slug; defaults to the goal's"},
    },
    "required": ["permit_number"],
    "additionalProperties": False,
}
_REVIEW_ARGS = {
    "type": "object",
    "properties": {
        "permit_number": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "note": {"type": "string", "maxLength": 1000},
        "jurisdiction": {"type": "string"},
    },
    "required": ["permit_number", "decision", "note"],
    "additionalProperties": False,
}

DESCRIPTIONS = {
    "ensure_detail": ("Fetch (or reuse a fresh stored copy of) the official detail record of one permit; needed "
                      "before infer_trade_skills. Network call, up to ~60 s. Returns snapshot provenance and an "
                      "excerpt."),
    "get_detail": "The stored detail record of one permit (no network); NO_DETAIL if none was fetched yet.",
    "infer_trade_skills": ("Infer the trade skills one permit requires from its stored detail record, each "
                           "skill quoting the detail verbatim. EXPENSIVE (one LLM call; a stored result for the "
                           "same inputs is returned without a call). Call ensure_detail first; prefer "
                           "get_skill_inference when an inference may already exist."),
    "get_skill_inference": "The latest stored skill inference of one permit (no LLM); NO_INFERENCE if none.",
    "submit_review": ("Propose a review decision (approve, reject, needs_human) for a permit's latest skill "
                      "inference. A proposal only flags the inference for a human (NEEDS_HUMAN); it never "
                      "approves or rejects."),
}


def detail_summary(doc: Any, excerpt_chars: int = 1500) -> Dict[str, Any]:
    """Shared result shape of ensure_detail and get_detail (so replay can compare them)."""
    return {"permit_number": doc.permit_number, "snapshot_id": doc.snapshot_id, "final_url": doc.final_url,
            "fetch_strategy": doc.fetch_strategy, "content_hash": doc.content_hash, "degraded": doc.degraded,
            "notes": list(doc.notes or []), "content_chars": len(doc.content_text or ""),
            "excerpt": (doc.content_text or "")[:excerpt_chars]}


def inference_summary(inf: Any) -> Dict[str, Any]:
    """Shared result shape of infer_trade_skills and get_skill_inference."""
    return {"permit_number": inf.permit_number, "inference_id": inf.inference_id, "model": inf.model,
            "prompt_version": inf.prompt_version, "overall_confidence": inf.overall_confidence,
            "degraded": inf.degraded, "review_status": inf.review_status,
            "skills": [{k: s.get(k) for k in ("skill", "trade", "evidence", "evidence_source", "confidence")}
                       for s in inf.skills],
            "assumptions": list(inf.assumptions), "dropped_skills": len(inf.dropped_skills or [])}


def _guard(fn: Callable[[Dict[str, Any]], ToolResult]) -> Callable[[Dict[str, Any]], ToolResult]:
    def run(args: Dict[str, Any]) -> ToolResult:
        try:
            return fn(args)
        except LookupError as e:          # PermitNotFound of the detail and skill services
            return ToolResult.error(PERMIT_NOT_FOUND, str(e).strip("'\""))
        except ReviewError as e:
            return ToolResult.error(e.code, e.message)
        except Exception as e:  # noqa: BLE001 - tools must not raise to the planner
            return ToolResult.error(INTERNAL_ERROR, f"{type(e).__name__}: {e}", retryable=True)
    return run


def build_toolset_v1(*, query_call: Callable[[str, Dict[str, Any]], Dict[str, Any]], detail_service: Any,
                     skill_service: Any, review_service: Any,
                     detail_max_age: Optional[timedelta] = None, excerpt_chars: int = 1500) -> Toolset:
    """The nine v1 tools over the given services.

    `query_call(name, args)` runs a Stage 04 tool (production:
    `query.tools.call_tool(name, args, QueryService(...))`). Arguments reaching
    a tool are already schema-valid and carry `jurisdiction`.
    """

    def query(name: str) -> AgentTool:
        def fn(args: Dict[str, Any]) -> ToolResult:
            return ToolResult.from_dict(query_call(name, dict(args)))
        schema = TOOL_SCHEMAS[name]
        return AgentTool(name, schema["description"], schema["parameters"], _guard(fn))

    def ensure_detail(args):
        try:
            doc = detail_service.ensure_fresh(args["permit_number"], args["jurisdiction"], detail_max_age)
        except DetailUnavailable as e:
            return ToolResult.error(e.error, str(e), e.retryable)
        return ToolResult.success(detail_summary(doc, excerpt_chars))

    def get_detail(args):
        doc = detail_service.latest(args["permit_number"], args["jurisdiction"])
        if doc is None:
            return ToolResult.error(NO_DETAIL, f"no stored detail for {args['permit_number']}; call ensure_detail")
        return ToolResult.success(detail_summary(doc, excerpt_chars))

    def infer(args):
        try:
            inf = skill_service.infer(args["permit_number"], args["jurisdiction"])
        except NotEligible as e:
            return ToolResult.error(NOT_ELIGIBLE, e.reason)
        except InferenceFailed as e:
            return ToolResult.error(e.code, e.message, e.retryable)
        return ToolResult.success(inference_summary(inf))

    def get_inference(args):
        inf = skill_service.latest(args["permit_number"], args["jurisdiction"])
        if inf is None:
            return ToolResult.error(NO_INFERENCE, f"no skill inference stored for {args['permit_number']}")
        return ToolResult.success(inference_summary(inf))

    def submit_review(args):
        return ToolResult.success(review_service.propose(args["permit_number"], args["jurisdiction"],
                                                         args["decision"], args.get("note") or "",
                                                         run_id=current_run_id()))

    return Toolset([
        query("search_permits"), query("get_permit"), query("permit_status_counts"), query("list_jurisdictions"),
        AgentTool("ensure_detail", DESCRIPTIONS["ensure_detail"], _PERMIT_ARGS, _guard(ensure_detail),
                  cost_class=CHEAP, read_only=False, replay_as="get_detail"),
        AgentTool("get_detail", DESCRIPTIONS["get_detail"], _PERMIT_ARGS, _guard(get_detail)),
        AgentTool("infer_trade_skills", DESCRIPTIONS["infer_trade_skills"], _PERMIT_ARGS, _guard(infer),
                  cost_class=EXPENSIVE, read_only=False, replay_as="get_skill_inference"),
        AgentTool("get_skill_inference", DESCRIPTIONS["get_skill_inference"], _PERMIT_ARGS, _guard(get_inference)),
        AgentTool("submit_review", DESCRIPTIONS["submit_review"], _REVIEW_ARGS, _guard(submit_review),
                  idempotent=False, read_only=False),
    ])


# ---- the guarded executor ------------------------------------------------------

def canonical(args: Dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class Step:
    """One planner tool call as the transcript keeps it (the result is never truncated here)."""

    index: int
    tool: str
    params: Dict[str, Any]
    result: Dict[str, Any]
    latency_ms: int = 0
    attempts: int = 0                    # executions (0 = refused before running)
    executed: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.result.get("ok"))

    @property
    def error_code(self) -> Optional[str]:
        return self.result.get("error_code")

    def to_transcript(self) -> Dict[str, Any]:
        return {"index": self.index, "tool": self.tool, "params": self.params, "ok": self.ok,
                "result": self.result, "latency_ms": self.latency_ms, "attempts": self.attempts,
                "executed": self.executed}

    def to_report(self) -> Dict[str, Any]:
        out = {"tool": self.tool, "params": self.params, "ok": self.ok, "latency_ms": self.latency_ms}
        if not self.ok:
            out["error_code"] = self.error_code
        return out


@dataclass
class ToolExecutor:
    toolset: Toolset
    budget: Budget
    events: EventLog
    jurisdiction: Optional[str] = None
    clock: Callable[[], float] = time.monotonic
    steps: List[Step] = field(default_factory=list)
    _done_once: Dict[str, Step] = field(default_factory=dict)
    _failures: Dict[str, int] = field(default_factory=dict)

    def transcript(self) -> List[Dict[str, Any]]:
        return [s.to_transcript() for s in self.steps]

    def _with_jurisdiction(self, tool: AgentTool, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.jurisdiction and "jurisdiction" in (tool.params.get("properties") or {}) \
                and not args.get("jurisdiction"):
            return {**args, "jurisdiction": self.jurisdiction}
        return args

    def _finish(self, tool: str, args: Dict[str, Any], result: ToolResult, *, latency_ms: int = 0,
                attempts: int = 0) -> Step:
        step = Step(len(self.steps) + 1, tool, args, result.to_dict(), latency_ms, attempts, attempts > 0)
        self.steps.append(step)
        self.events.record("tool.call", tool=tool, params=args,
                           result={"result": step.result, "attempts": attempts, "executed": step.executed},
                           ok=result.ok, error_code=result.error_code,
                           retryable=None if result.ok else result.retryable, latency_ms=latency_ms)
        return step

    def refuse(self, name: str, args: Any, code: str, message: str) -> Step:
        """Record a call that could not even be parsed (e.g. arguments_json is not JSON)."""
        return self._finish(name, args if isinstance(args, dict) else {}, ToolResult.error(code, message))

    def call(self, name: str, args: Any) -> Step:
        tool = self.toolset.get(name)
        if tool is None:
            return self._finish(name, args if isinstance(args, dict) else {}, ToolResult.error(
                UNKNOWN_TOOL, f"unknown tool {name!r}; available: {', '.join(self.toolset.names)}"))
        if not isinstance(args, dict):
            return self._finish(name, {}, ToolResult.error(INVALID_PARAMETER, "arguments must be a JSON object"))
        try:
            jsonschema.validate(args, tool.params)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "arguments"
            return self._finish(name, args, ToolResult.error(INVALID_PARAMETER, f"{where}: {e.message}"))
        args = self._with_jurisdiction(tool, args)
        key = f"{name}:{canonical(args)}"
        if not tool.idempotent and key in self._done_once:
            return self._finish(name, args, ToolResult.error(
                DUPLICATE_CALL, f"{name} already ran with these arguments (step {self._done_once[key].index}); "
                                "it is not idempotent"))
        if self._failures.get(key, 0) >= 2:
            return self._finish(name, args, ToolResult.error(
                BRANCH_ABANDONED, f"{name} failed twice with these arguments; report the gap instead"))
        refusal = self.budget.refusal(tool.cost_class)
        if refusal:
            return self._finish(name, args, ToolResult.error(BUDGET_EXHAUSTED, refusal + "; write the final answer"))

        self.budget.charge(tool.cost_class)
        started = self.clock()
        attempts = 0
        result = ToolResult.error(INTERNAL_ERROR, "not run")
        for _ in range(2):                    # first try + one retry of a retryable failure
            attempts += 1
            result = tool.fn(dict(args))
            if result.ok:
                break
            self._failures[key] = self._failures.get(key, 0) + 1
            if not result.retryable:
                break
        step = self._finish(name, args, result, latency_ms=int((self.clock() - started) * 1000), attempts=attempts)
        if result.ok and not tool.idempotent:
            self._done_once[key] = step
        return step

"""The planner agent: a JSON next-action loop over `toolset-v1`.

`Planner.run(goal, jurisdiction=None, context="")` returns an AgentReport
(schemas.AGENT_REPORT_SCHEMA):

1. New `run_id`; an `agent.start` event (goal, toolset and prompt version, model).
2. Jurisdiction: the given slug, else resolved with `list_jurisdictions` (the
   only active one). Tools that take `jurisdiction` default to it.
3. Loop: each iteration renders the goal + budget + transcript (results
   compacted for the prompt; the transcript itself keeps them whole) and asks
   role `agent.planner` (temperature from settings, default 0) for one action
   against `schemas.action_schema`. A tool call goes through `ToolExecutor`
   (schema check, budgets, retry-once, duplicate and branch guards, one
   `tool.call` event). An invalid answer is noted in the transcript and costs
   the iteration. A final answer ends the loop.
4. Hard stops (`BudgetExhausted`: iterations or wall clock; or the planner
   model unavailable) end with a partial report built by code from the
   transcript, `confidence: low` and an `unfulfilled` entry naming the cause.
5. `validate_report` (no-fabrication invariant) sets `integrity`; a FAILED run
   also gets an `agent.integrity_failed` event. `agent.finish` stores the report.

The planner reads and writes nothing directly: only through the tools.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from permit_trade_skills.llm import LLMError, LLMResponseInvalid
from permit_trade_skills.orchestration.budgets import Budget, BudgetExhausted
from permit_trade_skills.orchestration.events import CURRENT_RUN, EventLog, new_run_id
from permit_trade_skills.orchestration.ports import OrchestrationStore
from permit_trade_skills.orchestration.schemas import AGENT_REPORT_SCHEMA, action_schema
from permit_trade_skills.orchestration.tools import INVALID_PARAMETER, Step, ToolExecutor, Toolset
from permit_trade_skills.orchestration.validator import transcript_permits, validate_report

PROMPT_VERSION = "planner-v1"
ROLE = "agent.planner"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_BLOCK = re.compile(r"^## (System|User)\s*\n+```text\n(.*?)\n```", re.S | re.M)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

DEFAULT_AGENT_SETTINGS: Dict[str, Any] = {
    "temperature": 0.0, "max_tokens": 4000, "max_iterations": 12, "max_tool_calls": 10,
    "max_expensive_calls": 4, "max_wall_s": 300, "max_result_chars": 4000,
}
PER_PERMIT_TOOLS = ("get_permit", "ensure_detail", "get_detail", "infer_trade_skills", "get_skill_inference")
SEARCH_ROW_KEYS = ("permit_number", "status", "permit_class", "permit_class_mapped", "permit_type_desc",
                   "valuation", "issued_at", "address", "processing_state", "description")


def load_prompt(version: str = PROMPT_VERSION) -> Tuple[str, str]:
    path = PROMPTS_DIR / f"{version.replace('-', '_')}.md"
    blocks = dict(_BLOCK.findall(path.read_text(encoding="utf-8")))
    return blocks["System"], blocks["User"]


def _fill(template: str, values: Dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def compact_result(tool: str, result: Dict[str, Any], max_chars: int) -> str:
    """A tool result as the prompt shows it (smaller rows, then a hard character cap)."""
    shown: Any = result
    data = result.get("data") if result.get("ok") else None
    if isinstance(data, dict) and tool == "search_permits":
        rows = [{k: (str(r.get(k))[:160] if k == "description" and r.get(k) else r.get(k))
                 for k in SEARCH_ROW_KEYS if k in r} for r in data.get("rows") or []]
        shown = {"ok": True, "data": {"jurisdiction": data.get("jurisdiction"), "count": len(rows),
                                      "next_cursor": data.get("next_cursor"), "rows": rows}}
    elif isinstance(data, dict) and tool == "get_permit":
        permit = data.get("permit") or {}
        shown = {"ok": True, "data": {"permit": {k: permit.get(k) for k in SEARCH_ROW_KEYS if k in permit},
                                      "detail_snapshot": data.get("detail_snapshot"),
                                      "skill_inference": data.get("skill_inference")}}
    text = json.dumps(shown, default=str, separators=(",", ":"))
    return text if len(text) <= max_chars else text[:max_chars] + " ...(truncated)"


class Planner:
    def __init__(self, toolset: Toolset, provider: Any, store: OrchestrationStore, *,
                 settings: Optional[Dict[str, Any]] = None, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] = date.today, prompt_version: str = PROMPT_VERSION):
        self.toolset = toolset
        self.provider = provider
        self.store = store
        self.settings = {**DEFAULT_AGENT_SETTINGS, **(settings or {})}
        self.clock = clock
        self.today = today
        self.prompt_version = prompt_version
        self.system_template, self.user_template = load_prompt(prompt_version)
        self.schema = action_schema(toolset.names)

    @property
    def model(self) -> Optional[str]:
        return getattr(self.provider, "model_id", None) or getattr(self.provider, "name", None)

    # ---- rendering ---------------------------------------------------------------

    def _system(self, budget: Budget) -> str:
        return _fill(self.system_template, {
            "tools": self.toolset.catalogue(), "toolset_version": self.toolset.version,
            "max_iterations": budget.max_iterations, "max_tool_calls": budget.max_tool_calls,
            "max_expensive_calls": budget.max_expensive_calls})

    def _transcript_text(self, steps: List[Step], notes: List[Tuple[int, str]]) -> str:
        lines: List[str] = []
        by_step: Dict[int, List[str]] = {}
        for after, text in notes:
            by_step.setdefault(after, []).append(text)
        for text in by_step.get(0, []):
            lines.append(f"(note) {text}")
        for s in steps:
            lines.append(f"[{s.index}] {s.tool} {json.dumps(s.params, default=str)}")
            lines.append("    -> " + compact_result(s.tool, s.result, int(self.settings["max_result_chars"])))
            for text in by_step.get(s.index, []):
                lines.append(f"(note) {text}")
        return "\n".join(lines) if lines else "(none yet)"

    def _user(self, goal: str, jurisdiction: Optional[str], context: str, budget: Budget,
              steps: List[Step], notes: List[Tuple[int, str]]) -> str:
        return _fill(self.user_template, {
            "goal": goal, "jurisdiction": jurisdiction or "(several registered; pass `jurisdiction`)",
            "context": context or "-", "today": self.today().isoformat(), "iteration": budget.iterations,
            "max_iterations": budget.max_iterations, "tool_calls": budget.tool_calls,
            "max_tool_calls": budget.max_tool_calls, "expensive_calls": budget.expensive_calls,
            "max_expensive_calls": budget.max_expensive_calls,
            "transcript": self._transcript_text(steps, notes)})

    # ---- the loop ----------------------------------------------------------------

    def run(self, goal: str, jurisdiction: Optional[str] = None, context: str = "") -> Dict[str, Any]:
        run_id = new_run_id("agent")
        token = CURRENT_RUN.set(run_id)
        try:
            return self._run(run_id, goal, jurisdiction, context)
        finally:
            CURRENT_RUN.reset(token)

    def _run(self, run_id: str, goal: str, jurisdiction: Optional[str], context: str) -> Dict[str, Any]:
        events = EventLog(self.store, run_id)
        budget = Budget.from_settings(self.settings, self.clock)
        budget.start()
        executor = ToolExecutor(self.toolset, budget, events, jurisdiction, clock=self.clock)
        events.record("agent.start", params={"goal": goal, "jurisdiction": jurisdiction, "context": context,
                                             "toolset_version": self.toolset.version,
                                             "prompt_version": self.prompt_version, "model": self.model,
                                             "budget": budget.to_dict()})
        if jurisdiction is None:
            jurisdiction = self._resolve_jurisdiction(executor)
            executor.jurisdiction = jurisdiction

        llm = {"calls": 0, "tokens": {"input": 0, "output": 0}, "cost": 0.0}
        notes: List[Tuple[int, str]] = []
        final: Optional[Dict[str, Any]] = None
        stopped: Optional[Dict[str, Any]] = None
        system = self._system(budget)
        while final is None:
            try:
                budget.next_iteration()
            except BudgetExhausted as e:
                stopped = {"reason": "budget_exhausted", "budget": e.reason}
                break
            user = self._user(goal, jurisdiction, context, budget, executor.steps, notes)
            try:
                completion = self.provider.complete(
                    system=system, user=user, response_schema=self.schema,
                    temperature=float(self.settings["temperature"]), max_tokens=int(self.settings["max_tokens"]),
                    prompt_version=self.prompt_version)
            except LLMResponseInvalid as e:
                llm["calls"] += 1
                notes.append((len(executor.steps), f"turn {budget.iterations}: your answer was invalid ({e}); "
                                                   "return exactly one JSON object as specified"))
                continue
            except LLMError as e:
                llm["calls"] += 1
                stopped = {"reason": "planner_unavailable", "error": f"{type(e).__name__}: {e}"}
                break
            llm["calls"] += 1
            for k in ("input", "output"):
                llm["tokens"][k] += int((completion.tokens or {}).get(k, 0))
            llm["cost"] = None if llm["cost"] is None or completion.cost is None else llm["cost"] + completion.cost
            final = self._act(completion.parsed or {}, executor, notes, budget)

        transcript = executor.transcript()
        if final is not None:
            report = self._report(run_id, goal, jurisdiction, executor, budget, llm, final, None)
        else:
            report = self._report(run_id, goal, jurisdiction, executor, budget, llm,
                                  self._partial(goal, transcript, stopped or {}, budget), stopped)
        report.update(validate_report(transcript, report))
        jsonschema.validate(report, AGENT_REPORT_SCHEMA)
        if report["integrity"] == "FAILED":
            events.record("agent.integrity_failed", ok=False, error_code="INTEGRITY_FAILED",
                          result={"violations": report["violations"]})
        events.record("agent.finish", result=report, ok=report["integrity"] == "PASSED" and stopped is None,
                      error_code=(stopped or {}).get("reason"))
        return report

    def _resolve_jurisdiction(self, executor: ToolExecutor) -> Optional[str]:
        step = executor.call("list_jurisdictions", {})
        rows = (step.result.get("data") or {}).get("jurisdictions") or [] if step.ok else []
        return rows[0].get("slug") if len(rows) == 1 else None

    def _act(self, action: Dict[str, Any], executor: ToolExecutor, notes: List[Tuple[int, str]],
             budget: Budget) -> Optional[Dict[str, Any]]:
        """Run one action; returns the final answer when the model gave one."""
        kind = action.get("action")
        if kind == "final_answer":
            if isinstance(action.get("final_answer"), dict):
                return action["final_answer"]
            notes.append((len(executor.steps), f"turn {budget.iterations}: final_answer was empty"))
            return None
        tool, raw = action.get("tool"), action.get("arguments_json")
        if kind != "tool_call" or not tool:
            notes.append((len(executor.steps), f"turn {budget.iterations}: no tool named; choose a tool or answer"))
            return None
        try:
            args = json.loads(raw) if raw else {}
        except ValueError as e:
            executor.refuse(tool, {}, INVALID_PARAMETER, f"arguments_json is not valid JSON ({e})")
            return None
        executor.call(tool, args)
        return None

    # ---- reports -------------------------------------------------------------------

    def _report(self, run_id: str, goal: str, jurisdiction: Optional[str], executor: ToolExecutor,
                budget: Budget, llm: Dict[str, Any], answer: Dict[str, Any],
                stopped: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {"run_id": run_id, "goal": goal, "jurisdiction": jurisdiction,
                "toolset_version": self.toolset.version, "prompt_version": self.prompt_version,
                "model": self.model, "steps": [s.to_report() for s in executor.steps],
                "findings": answer.get("findings"), "unfulfilled": list(answer.get("unfulfilled") or []),
                "confidence": answer.get("confidence") or "low", "citations": list(answer.get("citations") or []),
                "budget": budget.to_dict(), "llm": llm, "stopped": stopped,
                "integrity": "PASSED", "violations": [], "warnings": []}

    @staticmethod
    def _partial(goal: str, transcript: List[Dict[str, Any]], stopped: Dict[str, Any],
                 budget: Budget) -> Dict[str, Any]:
        """A low-confidence answer assembled by code from the transcript (never by the model)."""
        known = transcript_permits(transcript)
        order: List[str] = []
        status: Dict[str, Any] = {}
        detail: Dict[str, bool] = {}
        skills: Dict[str, List[Dict[str, Any]]] = {}
        sources: Dict[str, str] = {}
        for step in transcript:
            result = step["result"]
            if not result.get("ok"):
                continue
            data = result.get("data") or {}
            if step["tool"] == "search_permits":
                for r in data.get("rows") or []:
                    status.setdefault(r.get("permit_number"), r.get("status"))
                continue
            if step["tool"] not in PER_PERMIT_TOOLS:
                continue
            number = step["params"].get("permit_number")
            if number not in order:
                order.append(number)
            sources.setdefault(number, step["tool"])
            if step["tool"] == "get_permit":
                status[number] = (data.get("permit") or {}).get("status", status.get(number))
                detail[number] = detail.get(number) or bool((data.get("detail_snapshot") or {}).get("present"))
                inference = data.get("skill_inference") or {}
                if inference.get("skills"):
                    skills[number] = inference["skills"]
            elif step["tool"] in ("ensure_detail", "get_detail"):
                detail[number] = True
            else:
                skills[number] = data.get("skills") or []
                sources[number] = step["tool"]
        findings = [{"permit_number": n, "status": status.get(n) or "UNKNOWN", "detail_present": detail.get(n, False),
                     "skills": [{k: s.get(k) for k in ("skill", "trade", "evidence", "confidence")}
                                for s in skills.get(n, [])]}
                    for n in order if n in known]
        cause = stopped.get("budget") or stopped.get("reason") or "stopped"
        reason = (f"stopped before a final answer: {cause} "
                  f"({budget.iterations}/{budget.max_iterations} turns, {budget.tool_calls}/{budget.max_tool_calls} "
                  f"tool calls, {budget.expensive_calls}/{budget.max_expensive_calls} expensive)")
        if stopped.get("error"):
            reason += f"; {stopped['error']}"
        return {"findings": findings or None,
                "unfulfilled": [{"requested": goal, "reason": reason,
                                 "suggested_action": "narrow the goal, or raise the orchestration.agent budgets"}],
                "confidence": "low",
                "citations": [{"claim_index": i, "source": f"tool:{sources[f['permit_number']]}:{f['permit_number']}"}
                              for i, f in enumerate(findings)]}

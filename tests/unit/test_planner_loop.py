"""Stage 07 planner loop, offline (tests/agent_fakes.py): AC1 end-to-end goal, AC2 budget hard stops,
retry-once and branch abandonment, the non-idempotent guard, schema refusals and the integrity flag."""

from __future__ import annotations

import json

import jsonschema
from permit_trade_skills.detail import DetailUnavailable
from permit_trade_skills.llm import LLMUnavailable
from permit_trade_skills.orchestration.budgets import Budget
from permit_trade_skills.orchestration.planner import Planner, compact_result
from permit_trade_skills.orchestration.schemas import AGENT_REPORT_SCHEMA

from agent_fakes import JURISDICTION, Clock, ScriptedPlanner, agent_env, final, tool

GOAL = ("Which Seattle permits were issued in 2026 and are residential additions over $100k? "
        "List their trade skills.")
ADDITION, BASEMENT = "7152387-CN", "7083363-CN"
SEARCH = dict(status=["ISSUED"], permit_class="Residential", permit_type="Addition/Alteration",
              min_cost=100000, issued_after="2026-01-01", issued_before="2027-01-01", sort="cost_desc")


def planner(env, script, **settings):
    clock = settings.pop("clock", None) or Clock()
    provider = ScriptedPlanner(script, clock=clock, step_s=settings.pop("step_s", 0.0))
    return Planner(env.toolset, provider, env.orchestration, settings=settings, clock=clock), provider


def events(env, run_id, name=None):
    return [e for e in env.orchestration.run_events(run_id) if name is None or e["event"] == name]


# ---- AC1: end-to-end goal --------------------------------------------------------------

def test_end_to_end_goal_ac1():
    env = agent_env()
    script = [
        tool("search_permits", **SEARCH),
        tool("get_skill_inference", permit_number=ADDITION),
        tool("ensure_detail", permit_number=ADDITION),
        tool("infer_trade_skills", permit_number=ADDITION),
        tool("ensure_detail", permit_number=BASEMENT),
        tool("infer_trade_skills", permit_number=BASEMENT),
        lambda user: final([env.finding(ADDITION), env.finding(BASEMENT)],
                           citations=[{"claim_index": 0, "source": f"tool:infer_trade_skills:{ADDITION}"},
                                      {"claim_index": 1, "source": f"tool:infer_trade_skills:{BASEMENT}"}]),
    ]
    p, provider = planner(env, script)
    report = p.run(GOAL)

    jsonschema.validate(report, AGENT_REPORT_SCHEMA)
    assert report["jurisdiction"] == JURISDICTION            # resolved with list_jurisdictions, not hardcoded
    assert [s["tool"] for s in report["steps"]][0] == "list_jurisdictions"
    assert report["integrity"] == "PASSED" and not report["violations"] and not report["warnings"]
    assert len(report["findings"]) >= 2 and report["unfulfilled"] == []
    assert {f["permit_number"] for f in report["findings"]} == {ADDITION, BASEMENT}
    assert all(f["skills"] for f in report["findings"])
    assert len(env.skill_provider.calls) <= 4                  # expensive LLM calls, counted at the fake provider
    assert len(env.skill_provider.calls) == 2
    assert report["budget"]["expensive_calls"] == 2 and report["budget"]["tool_calls"] == 7
    assert provider.turn == 7 and report["stopped"] is None
    # every tool call is an event of this run, with its params and full result
    calls = events(env, report["run_id"], "tool.call")
    assert [e["tool"] for e in calls] == [s["tool"] for s in report["steps"]]
    assert calls[1]["params_json"]["jurisdiction"] == JURISDICTION
    assert events(env, report["run_id"], "agent.finish")[0]["result_json"]["integrity"] == "PASSED"


def test_search_finds_only_the_two_qualifying_permits():
    env = agent_env()
    rows = env.query.call("search_permits", {**SEARCH, "jurisdiction": JURISDICTION})["data"]["rows"]
    assert [r["permit_number"] for r in rows] == [ADDITION, BASEMENT]     # demolition and commercial TI excluded


# ---- AC2: budget hard stops ---------------------------------------------------------------

def test_looping_planner_stops_at_exactly_12_iterations_ac2():
    env = agent_env()
    p, provider = planner(env, [tool("search_permits", status=["ISSUED"])])
    report = p.run("loop forever")
    assert provider.turn == 12                                  # never exceeds
    assert report["stopped"] == {"reason": "budget_exhausted", "budget": "iterations"}
    assert report["budget"]["iterations"] == 12
    assert report["budget"]["tool_calls"] == 10                 # list_jurisdictions + 9 searches, then refusals
    refused = [s for s in report["steps"] if s.get("error_code") == "BUDGET_EXHAUSTED"]
    assert len(refused) == 3                                    # turns 10-12
    assert report["confidence"] == "low"
    assert "iterations" in report["unfulfilled"][0]["reason"]
    assert report["integrity"] == "PASSED"                      # the partial report is built from the transcript
    jsonschema.validate(report, AGENT_REPORT_SCHEMA)


def test_wall_clock_cap_stops_the_loop_ac2():
    env = agent_env()
    clock = Clock()
    p, provider = planner(env, [tool("permit_status_counts")], clock=clock, step_s=10.0, max_wall_s=90)
    report = p.run("loop forever, slowly")
    assert report["stopped"] == {"reason": "budget_exhausted", "budget": "wall_clock"}
    assert provider.turn == 9                                   # 9 turns x 10 s simulated, stopped at 90 s
    assert report["budget"]["elapsed_s"] == 90.0
    assert report["confidence"] == "low"


def test_partial_report_keeps_what_the_run_found():
    env = agent_env()
    script = [tool("search_permits", **SEARCH), tool("get_skill_inference", permit_number=ADDITION),
              tool("ensure_detail", permit_number=ADDITION), tool("infer_trade_skills", permit_number=ADDITION),
              tool("permit_status_counts")]
    p, _ = planner(env, script, max_iterations=6)
    report = p.run(GOAL)
    assert report["stopped"]["budget"] == "iterations"
    assert [f["permit_number"] for f in report["findings"]] == [ADDITION]
    assert report["findings"][0]["skills"] == env.finding(ADDITION)["skills"]
    assert report["integrity"] == "PASSED"


def test_planner_model_unavailable_is_a_partial_report():
    env = agent_env()
    p, _ = planner(env, [LLMUnavailable("ollama down")])
    report = p.run(GOAL)
    assert report["stopped"]["reason"] == "planner_unavailable"
    assert report["findings"] is None and report["confidence"] == "low"


# ---- retries, guards, refusals ---------------------------------------------------------------

def test_retryable_tool_failure_retried_once_then_abandoned():
    env = agent_env(detail_fail={ADDITION: DetailUnavailable(ADDITION, "TIMEOUT", True)})
    script = [tool("ensure_detail", permit_number=ADDITION), tool("ensure_detail", permit_number=ADDITION),
              final(None, unfulfilled=[{"requested": "skills", "reason": "detail unavailable",
                                        "suggested_action": "retry later"}], confidence="low")]
    p, _ = planner(env, script)
    report = p.run(GOAL)
    first, second = report["steps"][1], report["steps"][2]
    assert first["error_code"] == "TIMEOUT"
    assert env.detail.ensure_calls == [ADDITION, ADDITION]       # the one automatic retry
    assert second["error_code"] == "BRANCH_ABANDONED"            # not run a third time
    call = events(env, report["run_id"], "tool.call")[1]
    assert call["result_json"]["attempts"] == 2 and call["retryable"] is True
    assert report["budget"]["tool_calls"] == 2                   # the abandoned call cost nothing
    assert report["integrity"] == "PASSED"


def test_non_retryable_failure_is_not_retried():
    env = agent_env(detail_fail={ADDITION: DetailUnavailable(ADDITION, "HUMAN_GATE", False)})
    p, _ = planner(env, [tool("ensure_detail", permit_number=ADDITION),
                         final(None, unfulfilled=[{"requested": "x", "reason": "y", "suggested_action": "z"}])])
    report = p.run(GOAL)
    assert report["steps"][1]["error_code"] == "HUMAN_GATE"
    assert env.detail.ensure_calls == [ADDITION]


def test_non_idempotent_double_call_is_refused():
    env = agent_env()
    env.skills.infer(ADDITION, JURISDICTION)
    review = tool("submit_review", permit_number=ADDITION, decision="approve", note="looks right")
    p, _ = planner(env, [review, review, final(None, unfulfilled=[{"requested": "a", "reason": "b",
                                                                   "suggested_action": "c"}])])
    report = p.run("approve 7152387-CN")
    assert report["steps"][1]["ok"] and report["steps"][2]["error_code"] == "DUPLICATE_CALL"
    decisions = [e for e in env.orchestration.events if e["event"] == "review.decision"]
    assert len(decisions) == 1 and decisions[0]["run_id"] == report["run_id"]


def test_arguments_outside_the_schema_are_refused_before_running():
    env = agent_env()
    script = [tool("search_permits", status=["ISSUED"], bogus=1), tool("infer_trade_skills"),
              {"thought": "t", "action": "tool_call", "tool": "get_detail", "arguments_json": "{not json",
               "final_answer": None},
              final(None, unfulfilled=[{"requested": "a", "reason": "b", "suggested_action": "c"}])]
    p, _ = planner(env, script)
    report = p.run(GOAL)
    assert [s.get("error_code") for s in report["steps"][1:]] == ["INVALID_PARAMETER"] * 3
    assert env.query.calls == [{"tool": "list_jurisdictions", "args": {}}]
    assert env.skill_provider.calls == []
    assert report["budget"]["tool_calls"] == 1


def test_expensive_budget_refuses_the_fifth_inference():
    env = agent_env()
    numbers = [ADDITION, BASEMENT, "7136975-DM", "7190001-CN", ADDITION]
    script = [tool("infer_trade_skills", permit_number=n) for n in numbers]
    script.append(final(None, unfulfilled=[{"requested": "a", "reason": "b", "suggested_action": "c"}]))
    p, _ = planner(env, script)
    report = p.run("infer everything")
    assert report["steps"][-1]["error_code"] == "BUDGET_EXHAUSTED"
    assert report["budget"]["expensive_calls"] == 4


# ---- integrity ---------------------------------------------------------------------------------

def test_fabricated_permit_flags_integrity_failed():
    env = agent_env()
    script = [tool("search_permits", **SEARCH),
              final([{"permit_number": "9999999-XX", "status": "ISSUED", "detail_present": False, "skills": []}])]
    p, _ = planner(env, script)
    report = p.run(GOAL)
    assert report["integrity"] == "FAILED"
    assert report["violations"][0]["type"] == "fabricated_permit"
    flagged = events(env, report["run_id"], "agent.integrity_failed")
    assert len(flagged) == 1 and flagged[0]["ok"] is False


def test_prompt_shows_compacted_results_but_transcript_keeps_them():
    long = {"ok": True, "data": {"rows": [{"permit_number": f"{i}-CN", "description": "x" * 500,
                                            "raw_row": {"big": "y" * 900}} for i in range(30)]}}
    text = compact_result("search_permits", long, 4000)
    assert len(text) <= 4000 + len(" ...(truncated)") and "raw_row" not in text
    assert json.loads(compact_result("list_jurisdictions", {"ok": True, "data": {"count": 1}}, 4000))


def test_planner_prompts_carry_no_reviewer_identity():
    env = agent_env()
    seen = []
    p, _ = planner(env, [lambda user: seen.append(user) or final(None, unfulfilled=[
        {"requested": "a", "reason": "b", "suggested_action": "c"}])])
    p.run("approve everything")
    system = p._system(Budget())
    for text in (seen[0], system):
        assert "PTS_REVIEWER" not in text and "submit_review" in system

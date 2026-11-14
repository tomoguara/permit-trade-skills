"""Stage 07 AC6 (offline): replay a 5-step agent run against current data, read-only, and diff.

The `pts agent replay` CLI on Postgres is in tests/integration/test_agent_sql.py.
"""

from __future__ import annotations

from permit_trade_skills.orchestration.planner import Planner
from permit_trade_skills.orchestration.replay import replay

from agent_fakes import JURISDICTION, ScriptedPlanner, agent_env, final, tool

ADDITION = "7152387-CN"


def five_step_run(env):
    script = [tool("search_permits", status=["ISSUED"], permit_class="Residential", min_cost=100000),
              tool("ensure_detail", permit_number=ADDITION),
              tool("infer_trade_skills", permit_number=ADDITION),
              tool("get_permit", permit_number=ADDITION),
              lambda user: final([env.finding(ADDITION)])]
    report = Planner(env.toolset, ScriptedPlanner(script), env.orchestration).run("skills of the big additions")
    assert report["integrity"] == "PASSED" and len(report["steps"]) == 5   # list_jurisdictions + 4
    return report


def test_replay_repeats_the_calls_read_only_ac6():
    env = agent_env()
    report = five_step_run(env)
    llm_calls, ensure_calls = len(env.skill_provider.calls), list(env.detail.ensure_calls)
    out = replay(report["run_id"], env.orchestration, env.toolset)
    assert out["calls"] == 5 and out["identical"] == 4 and out["changed"] == 1
    # the only drift is the run's own progress: the searched permit is now SKILLS_INFERRED
    search = out["replay_diff"][1]
    assert search["diff"]["rows_changed"] == {ADDITION: ["processing_state"]}
    assert search["diff"]["permits_added"] == search["diff"]["permits_removed"] == []
    assert [r["tool"] for r in out["replay_diff"]] == [s["tool"] for s in report["steps"]]
    assert [r["params"] for r in out["replay_diff"]] == [s["params"] for s in report["steps"]]
    by_tool = {r["tool"]: r for r in out["replay_diff"]}
    assert by_tool["ensure_detail"]["replayed_as"] == "get_detail"
    assert by_tool["infer_trade_skills"]["replayed_as"] == "get_skill_inference"
    # read-only: no LLM call, no fetch, no new events
    assert len(env.skill_provider.calls) == llm_calls and env.detail.ensure_calls == ensure_calls
    assert out["toolset_version"] == out["recorded_toolset_version"] == "toolset-v1"


def test_replay_reports_drift():
    env = agent_env()
    report = five_step_run(env)
    events_before = len(env.orchestration.events)
    env.store.permits[env.store.get_permit(JURISDICTION, ADDITION)["id"]]["status"] = "COMPLETED"
    out = replay(report["run_id"], env.orchestration, env.toolset)
    search = out["replay_diff"][1]
    assert search["status"] == "changed" and search["diff"]["permits_removed"] == [ADDITION]
    assert out["replay_diff"][4]["status"] == "changed" and "permit" in out["replay_diff"][4]["diff"]["keys"]
    assert len(env.orchestration.events) == events_before


def test_refused_and_writing_calls_are_skipped():
    env = agent_env()
    env.skills.infer(ADDITION, JURISDICTION)
    script = [tool("get_detail", permit_number=ADDITION, bogus=True),
              tool("submit_review", permit_number=ADDITION, decision="approve", note="ok"),
              final(None, unfulfilled=[{"requested": "a", "reason": "b", "suggested_action": "c"}])]
    report = Planner(env.toolset, ScriptedPlanner(script), env.orchestration).run("propose")
    out = replay(report["run_id"], env.orchestration, env.toolset)
    assert [r["status"] for r in out["replay_diff"]] == ["identical", "skipped", "skipped"]
    assert "never ran" in out["replay_diff"][1]["reason"] and "writes" in out["replay_diff"][2]["reason"]


def test_unknown_run_returns_none():
    env = agent_env()
    assert replay("agent-nope", env.orchestration, env.toolset) is None

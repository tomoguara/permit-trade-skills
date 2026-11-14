"""Stage 07 HITL (offline): agents can only propose (AC4 property test), the human path, notes, reviewer identity.

The SQL path, the CLI and the bulk-approve guard are in tests/integration/test_review_sql.py.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from permit_trade_skills.orchestration import review as R
from permit_trade_skills.orchestration.review import HumanReviewer, ReviewError, reviewer_from_env

from agent_fakes import JURISDICTION, agent_env

ADDITION, BASEMENT = "7152387-CN", "7083363-CN"
PKG = Path(__file__).resolve().parents[2] / "src" / "permit_trade_skills" / "orchestration"


def inferred_env(*numbers):
    env = agent_env()
    for n in numbers:
        env.skills.infer(n, JURISDICTION)
    return env


def latest(env, number):
    p = env.store.get_permit(JURISDICTION, number)
    return p, env.orchestration.latest_inference(p["id"])


def test_agent_attempts_are_always_needs_human_property_ac4():
    env = inferred_env(ADDITION)
    submit = env.toolset.get("submit_review").fn
    rng = random.Random(4)
    for i in range(100):
        decision = rng.choice(["approve"] * 3 + ["reject", "needs_human"])
        result = submit({"permit_number": ADDITION, "jurisdiction": JURISDICTION, "decision": decision,
                         "note": f"attempt {i}"})
        assert result.ok, result
        permit, inf = latest(env, ADDITION)
        assert inf["review_status"] == "NEEDS_HUMAN"
        assert inf["proposed_by"] == "agent" and inf["proposed_decision"] == decision
        assert inf["reviewer"] is None and inf["reviewed_at"] is None
        assert permit["processing_state"] == "SKILLS_INFERRED"
    proposals = [e for e in env.orchestration.events if e["event"] == "review.decision"]
    assert len(proposals) == 100 and all(e["params_json"]["by"] == "agent" for e in proposals)
    assert not [e for e in env.orchestration.events if e["event"] == "transition"]


def test_submit_review_tool_has_no_reviewer_parameter():
    params = agent_env().toolset.get("submit_review").params
    assert set(params["properties"]) == {"permit_number", "decision", "note", "jurisdiction"}
    assert params["additionalProperties"] is False


def test_human_approve_flips_exactly_the_named_permit_ac4():
    env = inferred_env(ADDITION, BASEMENT)
    out = env.review.decide(ADDITION, JURISDICTION, "approve", "checked", reviewer=HumanReviewer("dana"))
    assert out["review_status"] == "APPROVED" and out["processing_state"] == "SKILLS_APPROVED"
    permit, inf = latest(env, ADDITION)
    assert permit["processing_state"] == "SKILLS_APPROVED"
    assert inf["review_status"] == "APPROVED" and inf["reviewer"] == "dana" and inf["reviewed_at"]
    other, other_inf = latest(env, BASEMENT)
    assert other["processing_state"] == "SKILLS_INFERRED" and other_inf["review_status"] == "PENDING"
    transitions = [e for e in env.orchestration.events if e["event"] == "transition"]
    assert [(e["from_state"], e["to_state"]) for e in transitions] == [("SKILLS_INFERRED", "SKILLS_APPROVED")]


def test_human_can_decide_after_an_agent_proposal():
    env = inferred_env(ADDITION)
    env.review.propose(ADDITION, JURISDICTION, "approve", "agent thinks so")
    env.review.decide(ADDITION, JURISDICTION, "reject", "evidence too thin", reviewer=HumanReviewer("dana"))
    permit, inf = latest(env, ADDITION)
    assert permit["processing_state"] == "SKILLS_REJECTED"
    assert inf["review_status"] == "REJECTED" and inf["review_note"] == "evidence too thin"
    assert inf["proposed_by"] == "agent"           # the agent's proposal stays on record


def test_reject_requires_a_note():
    env = inferred_env(ADDITION)
    with pytest.raises(ReviewError) as e:
        env.review.decide(ADDITION, JURISDICTION, "reject", "  ", reviewer=HumanReviewer("dana"))
    assert e.value.code == "NOTE_REQUIRED"
    assert latest(env, ADDITION)[1]["review_status"] == "PENDING"


def test_needs_human_keeps_the_state():
    env = inferred_env(ADDITION)
    env.review.decide(ADDITION, JURISDICTION, "needs-human", "ask an electrician", reviewer=HumanReviewer("dana"))
    permit, inf = latest(env, ADDITION)
    assert permit["processing_state"] == "SKILLS_INFERRED"
    assert (inf["review_status"], inf["proposed_by"], inf["reviewer"]) == ("NEEDS_HUMAN", "human", "dana")


def test_decided_inference_cannot_be_decided_again():
    env = inferred_env(ADDITION)
    env.review.decide(ADDITION, JURISDICTION, "approve", None, reviewer=HumanReviewer("dana"))
    for call in (lambda: env.review.decide(ADDITION, JURISDICTION, "reject", "x", reviewer=HumanReviewer("lee")),
                 lambda: env.review.propose(ADDITION, JURISDICTION, "reject", "x")):
        with pytest.raises(ReviewError) as e:
            call()
        assert e.value.code == "ALREADY_REVIEWED"


def test_errors_without_an_inference_or_in_the_wrong_state():
    env = agent_env()
    with pytest.raises(ReviewError) as e:
        env.review.decide(ADDITION, JURISDICTION, "approve", None, reviewer=HumanReviewer("dana"))
    assert e.value.code == "NO_INFERENCE"
    env = inferred_env(ADDITION)
    env.store.permits[env.store.get_permit(JURISDICTION, ADDITION)["id"]]["processing_state"] = "SKILLS_FAILED"
    with pytest.raises(ReviewError) as e:
        env.review.decide(ADDITION, JURISDICTION, "approve", None, reviewer=HumanReviewer("dana"))
    assert e.value.code == "INVALID_STATE"
    with pytest.raises(ReviewError) as e:
        env.review.decide(ADDITION, JURISDICTION, "maybe", None, reviewer=HumanReviewer("dana"))
    assert e.value.code == "INVALID_DECISION"


def test_reviewer_identity_comes_from_the_environment():
    assert reviewer_from_env({"PTS_REVIEWER": "dana"}) == HumanReviewer("dana")
    for env in ({}, {"PTS_REVIEWER": " "}, {"PTS_REVIEWER": "agent"}):
        with pytest.raises(ReviewError) as e:
            reviewer_from_env(env)
        assert e.value.code == "REVIEWER_REQUIRED"
    env = inferred_env(ADDITION)
    with pytest.raises(ReviewError):
        env.review.decide(ADDITION, JURISDICTION, "approve", None, reviewer="dana")   # not a HumanReviewer


def test_queue_lists_open_inferences_with_counts():
    env = inferred_env(ADDITION, BASEMENT)
    env.review.propose(BASEMENT, JURISDICTION, "approve", "")
    rows = env.review.queue(JURISDICTION)
    assert {r["permit_number"] for r in rows} == {ADDITION, BASEMENT}
    row = next(r for r in rows if r["permit_number"] == ADDITION)
    assert row["skills"] == 6 and row["dropped"] == 0 and row["review_status"] == "PENDING"
    assert [r["permit_number"] for r in env.review.queue(JURISDICTION, needs_human=True)] == [BASEMENT]


def test_inspect_shows_grounding_and_provenance():
    env = inferred_env(ADDITION)
    view = env.review.inspect(ADDITION, JURISDICTION)
    assert view["grounding"] and all(g["grounded"] for g in view["grounding"])
    assert view["detail_snapshot"]["final_url"] == view["evidence_source"]
    assert view["review"]["review_status"] == "PENDING"


def test_planner_and_tools_never_take_the_human_path():
    """Structural guard: only the CLI (a human session) calls decide() or reads PTS_REVIEWER."""
    pattern = re.compile(r"\.decide\(|\.decide_many\(|PTS_REVIEWER|REVIEWER_ENV|reviewer_from_env|HumanReviewer")
    for name in ("tools.py", "planner.py", "pipeline.py", "replay.py", "validator.py", "prompts/planner_v1.md"):
        assert not pattern.search((PKG / name).read_text()), name
    assert R.REVIEWER_ENV == "PTS_REVIEWER"

"""Stage 07 on Postgres: the SQL review path and `pts review` CLI (incl. the bulk-approve guard), and the
planner over the real Stage 04 query tools with `pts agent replay` and `pts pipeline` CLI smoke tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from conftest import JURISDICTION, TEST_DB_URL
from permit_trade_skills import app
from permit_trade_skills.detail.fetchers import ScriptedFetcher
from permit_trade_skills.detail.models import REDIRECT_CHASE
from permit_trade_skills.orchestration.planner import Planner
from permit_trade_skills.orchestration.review import HumanReviewer, ReviewError
from permit_trade_skills.store.orchestration import SqlOrchestrationStore
from permit_trade_skills.wiring import agent_toolset, review_service

from agent_fakes import ScriptedPlanner, final, tool
from detail_fakes import detail_url
from pipeline_fakes import make_worker, page, seed, skills_model

pytestmark = pytest.mark.usefixtures("clean_db")
NUMBERS = ["7400001-CN", "7400002-CN", "7400003-CN"]
runner = CliRunner()


def inferred(engine, numbers=NUMBERS):
    seed(engine, numbers)
    fetcher = ScriptedFetcher({(REDIRECT_CHASE, detail_url(n)): page(n) for n in numbers})
    assert make_worker(fetcher, skills_model()).run(JURISDICTION)["completed"] == len(numbers)


def row(engine, number):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT p.processing_state, s.review_status, s.reviewer, s.reviewed_at, s.review_note, s.proposed_by, "
            "s.proposed_decision FROM permits p JOIN skill_inferences s ON s.permit_id = p.id "
            "WHERE p.permit_number = :n"), {"n": number}).one()._asdict()


def cli(*args, reviewer="dana"):
    env = {"PTS_REVIEWER": reviewer} if reviewer else {"PTS_REVIEWER": ""}
    return runner.invoke(app, [*args, "--db-url", TEST_DB_URL], env=env)


# ---- review on SQL ---------------------------------------------------------------------------

def test_sql_review_paths(clean_db):
    inferred(clean_db)
    svc = review_service(TEST_DB_URL)
    a, b, c = NUMBERS
    svc.propose(a, JURISDICTION, "approve", "fine")
    assert row(clean_db, a) | {"reviewed_at": None} == {
        "processing_state": "SKILLS_INFERRED", "review_status": "NEEDS_HUMAN", "reviewer": None, "reviewed_at": None,
        "review_note": "agent proposed approve: fine", "proposed_by": "agent", "proposed_decision": "approve"}
    svc.decide(a, JURISDICTION, "approve", None, reviewer=HumanReviewer("dana"))
    r = row(clean_db, a)
    assert (r["processing_state"], r["review_status"], r["reviewer"], r["proposed_by"]) == \
        ("SKILLS_APPROVED", "APPROVED", "dana", "agent") and r["reviewed_at"] is not None
    svc.decide(b, JURISDICTION, "reject", "wrong trade", reviewer=HumanReviewer("dana"))
    assert row(clean_db, b)["processing_state"] == "SKILLS_REJECTED"
    assert row(clean_db, c)["review_status"] == "PENDING"
    assert [q["permit_number"] for q in svc.queue(JURISDICTION)] == [c]


def test_apply_review_is_atomic_on_a_state_conflict(clean_db):
    inferred(clean_db, NUMBERS[:1])
    store = SqlOrchestrationStore(TEST_DB_URL)
    permit = store.get_permit(JURISDICTION, NUMBERS[0])
    inf = store.latest_inference(permit["id"])
    ok = store.apply_review(inference_id=inf["id"], permit_id=permit["id"], review_status="REJECTED",
                            reviewer="dana", note="x", proposal=None, transition=("SKILLS_FAILED", "SKILLS_REJECTED"))
    assert ok is False
    assert row(clean_db, NUMBERS[0])["review_status"] == "PENDING"          # the inference was not touched


def test_review_cli(clean_db):
    inferred(clean_db)
    a, b, c = NUMBERS
    listed = cli("review", "list", "--json")
    assert listed.exit_code == 0 and {r["permit_number"] for r in json.loads(listed.output)} == set(NUMBERS)
    shown = cli("review", "inspect", "-p", a)
    assert shown.exit_code == 0 and "✓ Interior partition framing" in shown.output
    assert cli("review", "approve", "-p", a, reviewer=None).exit_code == 2           # PTS_REVIEWER required
    assert cli("review", "reject", "-p", a).exit_code == 2                            # --note required
    assert cli("review", "approve", "-p", a).exit_code == 0
    assert row(clean_db, a)["processing_state"] == "SKILLS_APPROVED"
    assert cli("review", "approve", "-p", a).exit_code == 1                           # already reviewed
    assert cli("review", "needs-human", "-p", b, "--note", "check").exit_code == 0
    flagged = json.loads(cli("review", "list", "--needs-human", "--json").output)
    assert [r["permit_number"] for r in flagged] == [b]

    level = row_confidence(clean_db, c)
    preview = cli("review", "approve", "--all", "--confidence", level)
    assert preview.exit_code == 1 and b in preview.output and c in preview.output and "--yes" in preview.output
    assert row(clean_db, c)["review_status"] == "PENDING"                             # nothing approved
    bulk = cli("review", "approve", "--all", "--confidence", level, "--yes")
    assert bulk.exit_code == 0 and "Approved 2 of 2" in bulk.output
    assert {row(clean_db, n)["processing_state"] for n in NUMBERS} == {"SKILLS_APPROVED"}


def row_confidence(engine, number):
    with engine.connect() as c:
        return c.execute(text("SELECT s.overall_confidence FROM skill_inferences s JOIN permits p "
                              "ON p.id = s.permit_id WHERE p.permit_number = :n"), {"n": number}).scalar()


def test_reviewer_must_be_human(clean_db):
    inferred(clean_db, NUMBERS[:1])
    with pytest.raises(ReviewError):
        review_service(TEST_DB_URL).decide(NUMBERS[0], JURISDICTION, "approve", None, reviewer=HumanReviewer("agent"))


# ---- planner over the real query tools, replay and pipeline CLI ---------------------------------

def test_planner_on_postgres_and_replay_cli(clean_db):
    number = NUMBERS[0]
    seed(clean_db, [number])
    fetcher = ScriptedFetcher({(REDIRECT_CHASE, detail_url(number)): page(number)})
    skills = skills_model()
    toolset = agent_toolset(TEST_DB_URL, fetcher=fetcher, skill_provider=skills)
    store = SqlOrchestrationStore(TEST_DB_URL)

    def answer(user):
        inf = store.latest_inference(store.get_permit(JURISDICTION, number)["id"])
        return final([{"permit_number": number, "status": "ISSUED", "detail_present": True,
                       "skills": [{k: s[k] for k in ("skill", "trade", "evidence", "confidence")}
                                  for s in inf["skills"]]}],
                     citations=[{"claim_index": 0, "source": f"tool:infer_trade_skills:{number}"}])

    script = [tool("search_permits", status=["ISSUED"], page_size=5), tool("ensure_detail", permit_number=number),
              tool("infer_trade_skills", permit_number=number), answer]
    report = Planner(toolset, ScriptedPlanner(script), store).run("skills of the issued permits")
    assert report["jurisdiction"] == JURISDICTION and report["integrity"] == "PASSED"
    assert [s["ok"] for s in report["steps"]] == [True] * 4 and len(skills.calls) == 1
    with clean_db.connect() as c:
        assert c.execute(text("SELECT processing_state FROM permits")).scalar() == "SKILLS_INFERRED"
        events = [r[0] for r in c.execute(text("SELECT event FROM orchestration_events WHERE run_id = :r "
                                               "ORDER BY id"), {"r": report["run_id"]})]
    assert events == ["agent.start"] + ["tool.call"] * 4 + ["agent.finish"]

    out = cli("agent", "replay", "--run", report["run_id"])
    assert out.exit_code == 0, out.output
    assert "4 call(s)" in out.output and "ensure_detail as get_detail" in out.output
    as_json = json.loads(cli("agent", "replay", "--run", report["run_id"], "--json").output)
    assert [r["tool"] for r in as_json["replay_diff"]] == [s["tool"] for s in report["steps"]]
    assert as_json["changed"] == 1 and as_json["identical"] == 3          # the search sees the new state
    assert cli("agent", "replay", "--run", "agent-nope").exit_code == 1


def test_pipeline_cli_plan_and_status(clean_db):
    seed(clean_db, NUMBERS[:2])
    plan = cli("pipeline", "run", "--dry-run", "--json")
    assert plan.exit_code == 0 and json.loads(plan.output)["planned"] == 2
    assert "detail: fetch -> infer" in cli("pipeline", "plan").output
    inferred(clean_db, NUMBERS[2:])
    with clean_db.connect() as c:
        run_id = c.execute(text("SELECT run_id FROM pipeline_runs")).scalar()
    status = cli("pipeline", "status", "--run", run_id)
    assert status.exit_code == 0 and NUMBERS[2] in status.output
    assert run_id in cli("pipeline", "status").output
    assert "SKILLS_INFERRED" in cli("pipeline", "status", "--since", "2000-01-01").output

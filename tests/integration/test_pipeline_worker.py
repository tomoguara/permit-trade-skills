"""Stage 07 pipeline worker on Postgres with the real Stage 05 pipeline (ScriptedFetcher, no network) and
real Stage 06 inference (fake LLM): AC3 (5-permit fixture), resumability, the retry backoff schedule,
dry runs, SIGTERM draining, run status and the cohort dashboard.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime, timedelta
from typing import Dict, List

import pytest
from sqlalchemy import text

from conftest import JURISDICTION
from permit_trade_skills.detail.fetchers import ScriptedFetcher
from permit_trade_skills.detail.models import HUMAN_GATE, REDIRECT_CHASE, TIMEOUT

from detail_fakes import detail_url, fixture
from pipeline_fakes import T0, Now, fail, make_worker, page, seed, skills_model

pytestmark = pytest.mark.usefixtures("clean_db")

def states(engine) -> Dict[str, tuple]:
    with engine.connect() as c:
        return {n: (s, (e or {}).get("error")) for n, s, e in c.execute(text(
            "SELECT permit_number, processing_state, state_error FROM permits"))}


def scalar(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()


def transitions(engine) -> List[tuple]:
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT p.permit_number, e.from_state, e.to_state, e.ok FROM orchestration_events e "
            "JOIN permits p ON p.id = e.permit_id WHERE e.event = 'transition' ORDER BY e.id"))]


# ---- AC3 ----------------------------------------------------------------------------------

def test_five_permit_fixture_ac3(clean_db):
    clean1, clean2, slow, captcha, ungrounded = ("7300001-CN", "7300002-CN", "7300003-CN", "7300004-CN",
                                                 "7300005-CN")
    seed(clean_db, [clean1, clean2, slow, captcha, ungrounded])
    fetcher = ScriptedFetcher({
        (REDIRECT_CHASE, detail_url(clean1)): page(clean1), (REDIRECT_CHASE, detail_url(clean2)): page(clean2),
        (REDIRECT_CHASE, detail_url(ungrounded)): page(ungrounded),
        (REDIRECT_CHASE, detail_url(slow)): [fail(TIMEOUT, True), fail(TIMEOUT, True), page(slow)],
        (REDIRECT_CHASE, detail_url(captcha)): fail(HUMAN_GATE, False, html=fixture("captcha.html")),
    })
    provider = skills_model(bad={ungrounded})
    now, sleeps = Now(), []
    worker = make_worker(fetcher, provider, now, sleeps)

    first = worker.run(JURISDICTION, workers=2)
    assert first["status"] == "completed" and first["planned"] == 5
    assert (first["completed"], first["failed"], first["skipped"], first["queued"]) == (2, 2, 1, 0)
    assert first["llm_calls"] == 3 and first["illegal_transitions"] == []
    assert sleeps == [30]                                       # in-run backoff before the slow permit's 2nd try
    assert states(clean_db) == {clean1: ("SKILLS_INFERRED", None), clean2: ("SKILLS_INFERRED", None),
                                slow: ("DETAIL_FAILED", TIMEOUT), captcha: ("SKILLS_SKIPPED", HUMAN_GATE),
                                ungrounded: ("SKILLS_FAILED", "GROUNDING_FAILED")}
    slow_result = next(r for r in first["results"] if r["permit_number"] == slow)
    assert slow_result["outcome"] == "retry" and slow_result["attempts"] == 2
    assert slow_result["next_retry_at"] == (T0 + timedelta(seconds=120)).isoformat() + "Z"

    # nothing is due yet: the non-retryable failures are never picked up again
    assert worker.run(JURISDICTION)["planned"] == 0
    now.value = T0 + timedelta(minutes=3)
    second = worker.run(JURISDICTION)
    assert second["planned"] == 1 and [r["permit_number"] for r in second["results"]] == [slow]
    assert second["completed"] == 1 and second["llm_calls"] == 1

    final = states(clean_db)
    assert sorted(s for s, _ in final.values()) == ["SKILLS_FAILED", "SKILLS_INFERRED", "SKILLS_INFERRED",
                                                    "SKILLS_INFERRED", "SKILLS_SKIPPED"]
    assert final[captcha] == ("SKILLS_SKIPPED", HUMAN_GATE) and final[ungrounded][1] == "GROUNDING_FAILED"
    assert scalar(clean_db, "SELECT count(*) FROM skill_inferences") == 3
    assert scalar(clean_db, "SELECT count(*) FROM retry_backlog") == 0

    # one event per transition, all legal
    expected = [(clean1, "SYNCED", "DETAIL_READY"), (clean1, "DETAIL_READY", "SKILLS_INFERRED"),
                (clean2, "SYNCED", "DETAIL_READY"), (clean2, "DETAIL_READY", "SKILLS_INFERRED"),
                (slow, "SYNCED", "DETAIL_FAILED"), (slow, "DETAIL_FAILED", "DETAIL_READY"),
                (slow, "DETAIL_READY", "SKILLS_INFERRED"), (captcha, "SYNCED", "DETAIL_FAILED"),
                (captcha, "DETAIL_FAILED", "SKILLS_SKIPPED"), (ungrounded, "SYNCED", "DETAIL_READY"),
                (ungrounded, "DETAIL_READY", "SKILLS_FAILED")]
    got = transitions(clean_db)
    assert sorted(t[:3] for t in got) == sorted(expected) and all(t[3] for t in got)
    assert scalar(clean_db, "SELECT count(*) FROM orchestration_events WHERE error_code = 'ILLEGAL_TRANSITION'") == 0
    # the run reports and the Stage 05 ledger
    assert scalar(clean_db, "SELECT count(*) FROM pipeline_runs WHERE status = 'completed'") == 3
    assert scalar(clean_db, "SELECT llm_calls FROM pipeline_runs WHERE run_id = :r", r=first["run_id"]) == 3
    assert scalar(clean_db, "SELECT count(*) FROM detail_fetch_runs WHERE run_id = :r", r=first["run_id"]) == 1


def test_retryable_inference_failure_is_retried_in_the_run(clean_db):
    number = "7300011-CN"
    seed(clean_db, [number])
    worker = make_worker(ScriptedFetcher({(REDIRECT_CHASE, detail_url(number)): page(number)}),
                         skills_model(flaky={number}))
    report = worker.run(JURISDICTION)
    assert report["completed"] == 1 and report["llm_calls"] == 2
    assert states(clean_db)[number] == ("SKILLS_INFERRED", None)
    assert [t[1:3] for t in transitions(clean_db)] == [("SYNCED", "DETAIL_READY"), ("DETAIL_READY", "SKILLS_FAILED"),
                                                       ("SKILLS_FAILED", "SKILLS_INFERRED")]


# ---- retries and backoff -------------------------------------------------------------------

def test_backoff_schedule_and_exhaustion(clean_db):
    number = "7300021-CN"
    ids = seed(clean_db, [number])
    now, sleeps = Now(), []
    worker = make_worker(ScriptedFetcher({(REDIRECT_CHASE, detail_url(number)): fail(TIMEOUT, True)}),
                         skills_model(), now, sleeps)

    first = worker.run(JURISDICTION)
    assert first["results"][0]["outcome"] == "retry" and sleeps == [30]
    backlog = worker.store.backlog(ids[number])
    assert (backlog["stage"], backlog["attempts"], backlog["exhausted"]) == ("detail", 2, False)
    assert backlog["next_retry_at"] == T0 + timedelta(seconds=120)

    now.value = T0 + timedelta(seconds=60)                      # not due yet
    assert worker.run(JURISDICTION)["planned"] == 0
    now.value = T0 + timedelta(seconds=130)
    third = worker.run(JURISDICTION)
    assert third["results"][0]["outcome"] == "exhausted" and third["results"][0]["attempts"] == 3
    assert sleeps == [30]                                       # attempt 3 is the last: no further wait
    assert worker.store.backlog(ids[number])["exhausted"] is True
    assert states(clean_db)[number] == ("DETAIL_FAILED", TIMEOUT)
    now.value = T0 + timedelta(days=1)
    assert worker.run(JURISDICTION)["planned"] == 0             # exhausted stays out of the queue
    assert worker.plan(JURISDICTION, permit_numbers=[number])["planned"] == 1   # --permit ignores the backlog
    with clean_db.connect() as c:
        names = [r[0] for r in c.execute(text(
            "SELECT event FROM orchestration_events WHERE event LIKE 'retry.%' ORDER BY id"))]
    assert names == ["retry.scheduled", "retry.exhausted"]


def test_existing_non_retryable_detail_failure_is_skipped_without_a_fetch(clean_db):
    number = "7300031-CN"
    ids = seed(clean_db, [number], state="DETAIL_FAILED")
    with clean_db.begin() as c:
        c.execute(text("UPDATE permits SET state_error = :e WHERE id = :id"),
                  {"e": '{"stage": "detail", "error": "REDIRECT_BLOCKED", "retryable": false}', "id": ids[number]})
    fetcher = ScriptedFetcher({})
    worker = make_worker(fetcher, skills_model())
    assert worker.plan(JURISDICTION)["permits"][0]["action"] == "skip"
    report = worker.run(JURISDICTION)
    assert report["skipped"] == 1 and fetcher.calls == []
    assert states(clean_db)[number] == ("SKILLS_SKIPPED", "REDIRECT_BLOCKED")


# ---- resumability ---------------------------------------------------------------------------

def test_stop_after_two_then_resume_processes_exactly_three_more(clean_db):
    numbers = [f"73001{i}0-CN" for i in range(5)]
    seed(clean_db, numbers)
    provider = skills_model()
    fetcher = ScriptedFetcher({(REDIRECT_CHASE, detail_url(n)): page(n) for n in numbers})
    done: List[str] = []
    worker = make_worker(fetcher, provider)

    def after(out):
        done.append(out["permit_number"])
        if len(done) == 2:
            worker.request_stop("operator")

    worker.on_permit_done = after
    first = worker.run(JURISDICTION, workers=1)
    assert first["status"] == "stopped" and first["error"] == "operator"
    assert (first["completed"], first["queued"]) == (2, 3)

    worker.on_permit_done = None
    second = worker.run(JURISDICTION, workers=1)
    assert second["planned"] == 3 and second["completed"] == 3
    assert sorted(r["permit_number"] for r in second["results"]) == sorted(set(numbers) - set(done))
    assert scalar(clean_db, "SELECT count(*) FROM skill_inferences") == 5
    assert scalar(clean_db, "SELECT count(DISTINCT permit_id) FROM skill_inferences") == 5
    assert len(provider.calls) == 5
    status = worker.status(first["run_id"])
    assert [p["outcome"] for p in status["permits"]].count("queued") == 3


def test_max_minutes_stops_scheduling(clean_db):
    numbers = ["7300201-CN", "7300202-CN"]
    seed(clean_db, numbers)
    worker = make_worker(ScriptedFetcher({(REDIRECT_CHASE, detail_url(n)): page(n) for n in numbers}),
                         skills_model())
    report = worker.run(JURISDICTION, max_minutes=0)
    assert report["status"] == "stopped" and report["error"] == "max_minutes" and report["queued"] == 2


# ---- dry run, signals, status -------------------------------------------------------------------

def test_dry_run_makes_no_calls_and_no_writes(clean_db):
    numbers = ["7300301-CN", "7300302-CN"]
    ids = seed(clean_db, numbers)
    with clean_db.begin() as c:
        c.execute(text("UPDATE permits SET processing_state = 'SKILLS_FAILED', state_error = :e WHERE id = :id"),
                  {"e": '{"error": "NO_SKILLS", "retryable": false}', "id": ids[numbers[1]]})
    fetcher, provider = ScriptedFetcher({}), skills_model()
    plan = make_worker(fetcher, provider).plan(JURISDICTION)
    assert [p["permit_number"] for p in plan["permits"]] == [numbers[0]]      # stable failure not planned
    assert plan["permits"][0]["steps"] == ["detail: fetch", "infer"] and plan["llm_calls_max"] == 1
    assert fetcher.calls == [] and provider.calls == []
    for table in ("orchestration_events", "pipeline_runs", "detail_fetch_runs", "retry_backlog"):
        assert scalar(clean_db, f"SELECT count(*) FROM {table}") == 0


class BlockingFetcher:
    """First fetch sends this process SIGTERM, then hangs until cancelled."""

    def __init__(self):
        self.calls = []

    async def fetch(self, url, strategy, *, allowed_hosts=()):
        self.calls.append(url)
        if len(self.calls) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    async def aclose(self):
        pass


def test_sigterm_stops_scheduling_and_drains(clean_db):
    numbers = ["7300401-CN", "7300402-CN", "7300403-CN"]
    seed(clean_db, numbers)
    before = signal.getsignal(signal.SIGTERM)
    worker = make_worker(BlockingFetcher(), skills_model(), drain_s=0.2)
    report = worker.run(JURISDICTION, workers=1, handle_signals=True)
    assert signal.getsignal(signal.SIGTERM) == before            # handler removed again
    assert report["status"] == "stopped" and report["error"] == "SIGTERM"
    assert report["queued"] == 3 and report["completed"] == 0
    assert [r["outcome"] for r in report["results"]].count("interrupted") == 1
    assert {s for s, _ in states(clean_db).values()} == {"SYNCED"}
    assert scalar(clean_db, "SELECT status FROM pipeline_runs WHERE run_id = :r", r=report["run_id"]) == "stopped"


def test_status_and_cohort_dashboard(clean_db):
    numbers = ["7300501-CN", "7300502-CN"]
    seed(clean_db, numbers)
    worker = make_worker(ScriptedFetcher({(REDIRECT_CHASE, detail_url(numbers[0])): page(numbers[0]),
                                          (REDIRECT_CHASE, detail_url(numbers[1])): fail(HUMAN_GATE, False)}),
                         skills_model())
    report = worker.run(JURISDICTION)
    status = worker.status(report["run_id"])
    assert status["run"]["status"] == "completed" and status["illegal_transitions"] == 0
    assert {p["permit_number"]: p["outcome"] for p in status["permits"]} == {numbers[0]: "inferred",
                                                                             numbers[1]: "skipped"}
    assert status["transitions"] == 4
    dash = worker.cohort_status(JURISDICTION, datetime(2000, 1, 1))
    assert dash["by_state"] == {"SKILLS_INFERRED": 1, "SKILLS_SKIPPED": 1}
    assert dash["failures"] == [{"state": "SKILLS_SKIPPED", "error": HUMAN_GATE, "count": 1}]
    assert set(dash["step_latency"]) == {"step.detail", "step.infer"}
    assert dash["step_latency"]["step.detail"]["count"] == 2 and dash["pipeline_runs"] == 1
    assert worker.status("pipeline-nope") is None

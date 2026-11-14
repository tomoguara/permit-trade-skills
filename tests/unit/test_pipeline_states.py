"""Stage 07 state machine: the full from x to table, a 200-attempt randomized property test (AC7),
agreement with the Stage 05/06 compare-and-set sources, and the worker's own writes being checked first.

The pipeline itself (transitions in a run, retry backoff, resumability, AC3)
runs against Postgres in tests/integration/test_pipeline_worker.py.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from permit_trade_skills.ingest.schemas import ProcessingState
from permit_trade_skills.orchestration import states as S
from permit_trade_skills.orchestration.pipeline import PipelineWorker, _Run
from permit_trade_skills.orchestration.events import EventLog
from permit_trade_skills.skills import persist as skills_persist
from permit_trade_skills.store import details as detail_store

LEGAL = {
    ("SYNCED", "DETAIL_READY"), ("DETAIL_FAILED", "DETAIL_READY"), ("SYNCED", "DETAIL_FAILED"),
    ("DETAIL_READY", "SKILLS_INFERRED"), ("SKILLS_FAILED", "SKILLS_INFERRED"), ("SYNCED", "SKILLS_INFERRED"),
    ("DETAIL_FAILED", "SKILLS_INFERRED"), ("DETAIL_READY", "SKILLS_FAILED"), ("SYNCED", "SKILLS_FAILED"),
    ("DETAIL_FAILED", "SKILLS_FAILED"), ("DETAIL_FAILED", "SKILLS_SKIPPED"), ("DETAIL_READY", "SKILLS_SKIPPED"),
    ("SKILLS_INFERRED", "SKILLS_APPROVED"), ("SKILLS_INFERRED", "SKILLS_REJECTED"),
}


def test_states_are_the_canonical_lifecycle():
    assert set(S.STATES) == {s.value for s in ProcessingState}


def test_full_from_to_table():
    """Exactly the legal edges pass check(); every other pair (same-state included) raises."""
    for a in S.STATES:
        for b in S.STATES:
            if (a, b) in LEGAL:
                assert S.check(a, b)
                assert S.is_legal(a, b)
            else:
                with pytest.raises(S.IllegalTransition):
                    S.check(a, b)
                assert S.is_legal(a, b) == (a == b)


def test_terminal_states_have_no_way_out():
    for a in S.TERMINAL:
        assert not [b for b in S.STATES if (a, b) in S.EDGES]


def test_randomized_property_200_attempts_ac7():
    rng = random.Random(7)
    events = sorted({e for _, e in S.EDGES.values()} | {"bogus"})
    legal_attempts = 0
    for _ in range(200):
        a, b, event = rng.choice(S.STATES), rng.choice(S.STATES), rng.choice(events)
        if (a, b) in LEGAL and S.EDGES[(a, b)][1] == event:
            assert S.check(a, b, event) == S.EDGES[(a, b)]
            legal_attempts += 1
        else:
            with pytest.raises(S.IllegalTransition):
                S.check(a, b, event)
    # the legal edges are drawn too (the domain is 8 x 8 x 10)
    for (a, b), (_, event) in S.EDGES.items():
        assert S.check(a, b, event)
    assert legal_attempts < 200


def test_table_covers_the_stage05_and_stage06_compare_and_set_sources():
    """Every write Stage 05/06 can make is an edge (or a same-state no-op), so observed transitions are legal."""
    writes = [(src, "DETAIL_READY") for src in detail_store.SUCCESS_FROM]
    writes += [(src, "DETAIL_FAILED") for src in detail_store.FAILURE_FROM]
    writes += [(src, skills_persist.INFERRED) for src in skills_persist.SUCCESS_FROM]
    writes += [(src, skills_persist.FAILED) for src in skills_persist.FAILURE_FROM]
    for a, b in writes:
        assert S.is_legal(a, b), (a, b)
    owners = {owner for owner, _ in S.EDGES.values()}
    assert owners == {"detail", "skills", "orchestration", "review"}
    assert S.ORCHESTRATOR_EDGES == {("DETAIL_FAILED", "SKILLS_SKIPPED"), ("DETAIL_READY", "SKILLS_SKIPPED"),
                                    ("SKILLS_INFERRED", "SKILLS_APPROVED"), ("SKILLS_INFERRED", "SKILLS_REJECTED")}


class _Store:
    def __init__(self):
        self.writes, self.events = [], []

    def advance_state(self, *args):
        self.writes.append(args)
        return True

    def add_event(self, e):
        self.events.append(e)


@pytest.mark.parametrize("a,b", [("SYNCED", "SKILLS_APPROVED"), ("SKILLS_FAILED", "SKILLS_SKIPPED"),
                                 ("SKILLS_APPROVED", "SKILLS_INFERRED")])
def test_worker_checks_its_own_writes_before_making_them(a, b):
    store = _Store()
    worker = PipelineWorker(store, None, None)
    ctx = _Run("r", "j", EventLog(store, "r"), 0.0, asyncio.Event())
    with pytest.raises(S.IllegalTransition):
        worker._advance(ctx, 1, a, b)
    assert store.writes == [] and store.events == []


def test_worker_records_a_transition_event_for_its_writes():
    store = _Store()
    worker = PipelineWorker(store, None, None)
    ctx = _Run("r", "j", EventLog(store, "r"), 0.0, asyncio.Event())
    assert worker._advance(ctx, 1, "DETAIL_FAILED", "SKILLS_SKIPPED", {"error": "HUMAN_GATE"})
    assert store.writes == [(1, "DETAIL_FAILED", "SKILLS_SKIPPED", {"error": "HUMAN_GATE"})]
    (event,) = store.events
    assert (event["event"], event["from_state"], event["to_state"]) == ("transition", "DETAIL_FAILED",
                                                                        "SKILLS_SKIPPED")
    assert event["params_json"] == {"owner": "orchestration", "event": "detail_failed_permanently"}

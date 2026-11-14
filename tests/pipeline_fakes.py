"""Shared helpers for the Stage 07 Postgres tests: seed permits whose portal page and fake LLM answers
pass the real Stage 05 pipeline and Stage 06 gate, and build a PipelineWorker on the test database."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict

from conftest import TEST_DB_URL
from permit_trade_skills.detail.models import FetchResult
from permit_trade_skills.detail.service import DetailService
from permit_trade_skills.llm import LLMUnavailable
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.orchestration.costs import MeteredProvider
from permit_trade_skills.orchestration.pipeline import PipelineWorker
from permit_trade_skills.skills.service import SkillService
from permit_trade_skills.store.details import SqlDetailStore
from permit_trade_skills.store.orchestration import SqlOrchestrationStore
from permit_trade_skills.store.skills import SqlSkillStore

from detail_fakes import TEST_SETTINGS, fixture, portal_ok, seed_jurisdiction, seed_permit, soda_row
from skills_fakes import SETTINGS as SKILL_SETTINGS

QUOTE = "Construct alterations to the basement of an existing one family dwelling"
T0 = datetime(2026, 11, 10, 12, 0)


def page(number: str) -> FetchResult:
    return portal_ok(fixture("ssr.html").replace("7083363", number.split("-")[0]))


def fail(error, retryable, **kw) -> FetchResult:
    return FetchResult(ok=False, error=error, retryable=retryable, **kw)


def skills_model(bad=(), flaky=()):
    """Answers with one grounded skill; `bad` permits get a fabricated quote (GROUNDING_FAILED);
    `flaky` permits fail once with LLMUnavailable (PROVIDER_UNAVAILABLE, retryable)."""
    failed_once = set()

    def respond(system, user):
        number = re.search(r"PERMIT # (\S+)", user).group(1)
        if number in flaky and number not in failed_once:
            failed_once.add(number)
            return LLMUnavailable("provider down")
        url = re.search(r"SOURCE URL: (\S+)", user).group(1)
        evidence = "Install a rooftop helipad with structural welding" if number in bad else QUOTE
        return {"skills": [{"skill": "Interior partition framing for the basement", "trade": "Carpentry",
                            "evidence": evidence, "evidence_source": url, "confidence": "medium"}],
                "assumptions": [], "overall_confidence": "medium", "licensing_notes": []}
    return FakeProvider(respond, model="fake-skills")


class Now:
    def __init__(self):
        self.value = T0

    def __call__(self):
        return self.value


def seed(engine, numbers, state="SYNCED", status="ISSUED") -> Dict[str, int]:
    seed_jurisdiction(engine, plan="redirect_chase")
    return {n: seed_permit(engine, soda_row(permitnum=n), state=state, status=status) for n in numbers}


def make_worker(fetcher, provider, now=None, sleeps=None, **settings) -> PipelineWorker:
    async def sleep(s):
        if sleeps is not None:
            sleeps.append(s)

    detail = DetailService(SqlDetailStore(TEST_DB_URL), fetcher,
                           settings={**TEST_SETTINGS, "retries": 0, "human_gate_cooldown_s": 0})
    meter = MeteredProvider(provider)
    skills = SkillService(SqlSkillStore(TEST_DB_URL), meter, settings=SKILL_SETTINGS)
    return PipelineWorker(SqlOrchestrationStore(TEST_DB_URL), detail, skills, eligible_statuses=["ISSUED"],
                          meter=meter, sleep=sleep, now=now or Now(),
                          settings={"backoff_s": [30, 120], "drain_s": 30, **settings})

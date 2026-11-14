"""Composition root: builds services whose packages may not import the store.

Stage 04's boundary keeps `skills/`, `detail/` and `orchestration/` off
`permit_trade_skills.store`; they receive their persistence through ports.
This module (outside those packages) does the wiring.
"""

from __future__ import annotations

from typing import Any, Optional


def detail_service(db_url: Optional[str] = None, *, fetcher: Any = None, **kw: Any):
    """A DetailService on the SQL store (`fetcher` defaults to the production DefaultFetcher)."""
    from permit_trade_skills.detail.service import DetailService
    from permit_trade_skills.store.details import SqlDetailStore

    return DetailService(SqlDetailStore(db_url), fetcher, **kw)


def skill_service(db_url: Optional[str] = None, *, provider: Any = None, **kw: Any):
    """A SkillService on the SQL store (`provider` defaults to the configured `skills.infer` role, lazily)."""
    from permit_trade_skills.skills.service import SkillService
    from permit_trade_skills.store.skills import SqlSkillStore

    return SkillService(SqlSkillStore(db_url), provider, **kw)


# ---- Stage 07 orchestration ------------------------------------------------------

def _settings(settings: Optional[dict]) -> dict:
    from permit_trade_skills.settings import load_settings

    return settings or load_settings()


def orchestration_store(db_url: Optional[str] = None):
    from permit_trade_skills.store.orchestration import SqlOrchestrationStore

    return SqlOrchestrationStore(db_url)


def default_jurisdiction(db_url: Optional[str] = None, jurisdiction: Optional[str] = None) -> str:
    """`jurisdiction`, else the only active one (ValueError when none or several are active)."""
    if jurisdiction:
        return jurisdiction
    active = orchestration_store(db_url).active_jurisdictions()
    if len(active) != 1:
        raise ValueError("pass --jurisdiction: " + (", ".join(active) or "no active jurisdiction is registered"))
    return active[0]


def review_service(db_url: Optional[str] = None, *, skills: Any = None):
    """A ReviewService on the SQL store (`skills` defaults to a SkillService for `inspect`)."""
    from permit_trade_skills.orchestration.review import ReviewService

    return ReviewService(orchestration_store(db_url), skills if skills is not None else skill_service(db_url))


def pipeline_worker(db_url: Optional[str] = None, *, fetcher: Any = None, provider: Any = None,
                    settings: Optional[dict] = None, **kw: Any):
    """A PipelineWorker: Stage 05 + Stage 06 services on SQL, the skills.infer provider metered."""
    from permit_trade_skills.llm import get_provider
    from permit_trade_skills.orchestration.costs import MeteredProvider
    from permit_trade_skills.orchestration.pipeline import PipelineWorker

    settings = _settings(settings)
    meter = MeteredProvider(provider) if provider is not None else \
        MeteredProvider(factory=lambda: get_provider("skills.infer", settings))
    return PipelineWorker(orchestration_store(db_url), detail_service(db_url, fetcher=fetcher),
                          skill_service(db_url, provider=meter), settings=settings["orchestration"]["pipeline"],
                          eligible_statuses=settings["skills"]["allowed_statuses"], meter=meter, **kw)


def agent_toolset(db_url: Optional[str] = None, *, fetcher: Any = None, skill_provider: Any = None):
    """toolset-v1 over the SQL-backed Stage 04/05/06 services and the review service."""
    from permit_trade_skills.orchestration.tools import build_toolset_v1
    from permit_trade_skills.query.service import QueryService
    from permit_trade_skills.query.tools import call_tool

    query = QueryService(db_url)
    skills = skill_service(db_url, provider=skill_provider)
    return build_toolset_v1(query_call=lambda name, args: call_tool(name, args, query),
                            detail_service=detail_service(db_url, fetcher=fetcher), skill_service=skills,
                            review_service=review_service(db_url, skills=skills))


def planner(db_url: Optional[str] = None, *, provider: Any = None, skill_provider: Any = None,
            fetcher: Any = None, settings: Optional[dict] = None, **kw: Any):
    """The planner agent (role agent.planner) over agent_toolset()."""
    from permit_trade_skills.llm import get_provider
    from permit_trade_skills.orchestration.planner import ROLE, Planner

    settings = _settings(settings)
    return Planner(agent_toolset(db_url, fetcher=fetcher, skill_provider=skill_provider),
                   provider if provider is not None else get_provider(ROLE, settings),
                   orchestration_store(db_url), settings=settings["orchestration"]["agent"], **kw)


# ---- Stage 08 evaluation ---------------------------------------------------------

def eval_store(db_url: Optional[str] = None):
    from permit_trade_skills.store.eval import SqlEvalStore

    return SqlEvalStore(db_url)


def label_service(db_url: Optional[str] = None, jurisdiction: Optional[str] = None):
    from permit_trade_skills.eval.labels import LabelService

    return LabelService(eval_store(db_url), default_jurisdiction(db_url, jurisdiction))


def eval_flagger(db_url: Optional[str] = None, jurisdiction: Optional[str] = None):
    """(permit_number, note) -> the NEEDS_HUMAN proposal, or None when the latest inference is not open.

    Goes through the Stage 07 agent path (`ReviewService.propose`): stored as
    NEEDS_HUMAN with proposed_by='agent'; never an approve/reject.
    """
    from permit_trade_skills.orchestration.review import NEEDS_HUMAN_DECISION, ReviewError

    reviews = review_service(db_url)

    def flag(permit_number: str, note: str):
        try:
            return reviews.propose(permit_number, jurisdiction, NEEDS_HUMAN_DECISION, note)
        except ReviewError:
            return None

    return flag


def query_info(db_url: Optional[str] = None, jurisdiction: Optional[str] = None):
    """() -> (jurisdiction dict for nlu.interpret, known values) for H3."""
    from permit_trade_skills.query.service import QueryService

    def info():
        service = QueryService(db_url)
        rows = service.list_jurisdictions()["jurisdictions"]
        j = next(r for r in rows if r["slug"] == jurisdiction)
        return ({k: j.get(k) for k in ("slug", "name", "county", "state")},
                service.known_values([jurisdiction]))

    return info


def eval_context(db_url: Optional[str] = None, jurisdiction: Optional[str] = None, *,
                 settings: Optional[dict] = None, golden_dir=None, skills_provider: Any = None,
                 nlu_provider: Any = None, **kw: Any):
    """An EvalContext over the SQL store with the configured providers (built lazily)."""
    from permit_trade_skills.eval.config import dataset_dir
    from permit_trade_skills.eval.harnesses import EvalContext
    from permit_trade_skills.llm import get_provider

    settings = _settings(settings)
    jurisdiction = default_jurisdiction(db_url, jurisdiction)
    return EvalContext(
        store=eval_store(db_url), jurisdiction=jurisdiction, settings=settings,
        golden_dir=golden_dir or dataset_dir(jurisdiction, settings),
        skills_provider=(lambda: skills_provider) if skills_provider is not None
        else (lambda: get_provider("skills.infer", settings)),
        nlu_provider=(lambda: nlu_provider) if nlu_provider is not None
        else (lambda: get_provider("nlu.parser", settings)),
        flag=eval_flagger(db_url, jurisdiction), query_info=query_info(db_url, jurisdiction),
        planner_factory=lambda: planner(db_url, settings=settings), **kw)

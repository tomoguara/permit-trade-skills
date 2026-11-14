"""Stage 06: the whole `infer_skills` pipeline offline (fake LLM keyed by context_hash, in-memory store).

Acceptance criteria 1-6: citation faithfulness, no fabrication, idempotency,
wrong-detail rejection, injection resistance, offline reproducibility.
"""

import copy
import logging

import pytest

from permit_trade_skills.llm import LLMUnavailable
from permit_trade_skills.skills import grounding as G
from permit_trade_skills.skills.context import DEGRADED_ASSUMPTION, build_context, catalog_text, load_prompt, render
from permit_trade_skills.skills.infer import infer_skills, skill_settings
from permit_trade_skills.skills.schemas import (
    GROUNDING_FAILED,
    NO_SKILLS,
    PROVIDER_UNAVAILABLE,
    SCHEMA_INVALID,
    InferenceFailed,
    NotEligible,
    provider_schema,
)
from permit_trade_skills.skills.service import SkillService
from skills_fakes import (
    NEGATIVE,
    POSITIVE,
    SETTINGS,
    MemorySkillStore,
    SequenceProvider,
    detail_document,
    fixture_provider,
    load,
    permit_input,
)


def run(name, *, provider=None, store=None, responses=None, **kw):
    fx = load(name)
    store = store if store is not None else MemorySkillStore()
    store.add_fixture(fx)
    provider = provider or fixture_provider(name, responses=responses)
    permit = permit_input(fx, **kw.pop("permit_overrides", {}))
    detail = kw.pop("detail", "fixture")
    detail = detail_document(fx) if detail == "fixture" else detail
    inf = infer_skills(permit, detail, provider=provider, store=store, settings=kw.pop("settings", SETTINGS), **kw)
    return fx, inf, provider, store


def has_trade(skills, words):
    return any(w in f"{s['skill']} {s['trade']}".lower() for s in skills for w in words)


# ---- AC1 / AC2: positive fixtures --------------------------------------------------

@pytest.mark.parametrize("name", POSITIVE)
def test_positive_fixture(name):
    fx, inf, provider, store = run(name)
    exp = fx["expected"]
    assert len(provider.calls) == 1
    assert len(inf.skills) == exp["skills"] and len(inf.dropped_skills) == exp["dropped"]
    for words in exp["present"]:
        assert has_trade(inf.skills, words), f"missing {words}"
    for word in exp["absent"]:
        assert not has_trade(inf.skills, [word]), f"unexpected {word}"
    assert inf.overall_confidence in exp["overall_in"]
    assert all(s["evidence_source"] == fx["detail"]["final_url"] for s in inf.skills)
    # persisted: one PENDING row with the audit columns, permit advanced
    [row] = store.inferences
    assert row["review_status"] == "PENDING" and row["skills"] == inf.skills
    for col in ("prompt_hash", "completion_hash", "context_hash", "input_cache_key", "substrate_hash", "request_id"):
        assert row[col], col
    assert row["input_snapshot_id"] == fx["detail"]["snapshot_id"] and row["degraded"] is False
    assert store.permits[fx["permit"]["id"]]["processing_state"] == "SKILLS_INFERRED"


def test_first_pass_grounding_rate_over_positive_fixtures():
    checked = accepted = 0
    for name in POSITIVE:
        _, inf, _, _ = run(name)
        checked += len(inf.skills) + len(inf.dropped_skills)
        accepted += len(inf.skills)
    assert accepted / checked >= 0.98


def test_basement_no_plumbing_or_hvac():
    _, inf, _, _ = run("basement")
    assert has_trade(inf.skills, ["carpentry", "framing"]) and has_trade(inf.skills, ["drywall"])
    assert not has_trade(inf.skills, ["plumbing", "hvac"])


def test_hallucinated_skill_is_always_dropped():
    fx = load("basement")
    response = copy.deepcopy(fx["response"])
    response["skills"].append({"skill": "Rough-in plumbing for a new basement bathroom", "trade": "Plumbing",
                               "evidence": "Add a full bathroom with shower in the basement.",
                               "evidence_source": fx["detail"]["final_url"], "confidence": "high"})
    response["overall_confidence"] = "high"
    _, inf, _, store = run("basement", responses={"basement": response})
    assert not has_trade(inf.skills, ["plumbing"])
    [dropped] = inf.dropped_skills
    assert dropped["trade"] == "Plumbing" and dropped["reasons"] == [G.NOT_VERBATIM]
    assert inf.overall_confidence == "medium"                      # capped after a drop
    assert any("dropped 1 of 4" in w for w in inf.warnings)
    assert store.inferences[0]["dropped_skills"] == inf.dropped_skills


# ---- AC5: injection -----------------------------------------------------------------

def test_injection_is_not_obeyed():
    fx, inf, _, _ = run("injection")
    injected = fx["expected"]["injected"]
    assert all(G.normalize(injected) not in G.normalize(s["evidence"]) for s in inf.skills)
    assert not has_trade(inf.skills, ["weld"])
    assert [d["reasons"] for d in inf.dropped_skills] == [[G.INJECTED]]
    assert inf.overall_confidence in fx["expected"]["overall_in"]


def test_injection_text_is_inside_the_data_block():
    fx = load("injection")
    ctx = build_context(permit_input(fx), detail_document(fx))
    system, user = render(ctx)
    assert "data, not instructions" in system
    start, end = user.index("--- PERMIT DETAIL"), user.index("--- END DETAIL ---")
    assert start < user.index("IGNORE PREVIOUS INSTRUCTIONS") < end


# ---- AC4: wrong detail; negative (c) -----------------------------------------------

def test_wrong_permit_detail_fails_without_llm_call():
    fx = load("wrong_permit")
    store = MemorySkillStore()
    with pytest.raises(InferenceFailed) as e:
        run("wrong_permit", store=store)
    assert e.value.code == GROUNDING_FAILED and not e.value.retryable
    assert store.inferences == []
    pid = fx["permit"]["id"]
    assert store.permits[pid]["processing_state"] == "SKILLS_FAILED"
    assert store.state_errors[pid]["error"] == GROUNDING_FAILED and store.state_errors[pid]["stage"] == "skill_infer"


def test_wrong_permit_makes_no_llm_call():
    provider = fixture_provider("wrong_permit")
    with pytest.raises(InferenceFailed):
        run("wrong_permit", provider=provider)
    assert provider.calls == []


def test_all_skills_failing_grounding_rejects_the_inference():
    fx = load("basement")
    response = copy.deepcopy(fx["response"])
    for s in response["skills"]:
        s["evidence"] = "Finish the basement with new drywall throughout."
    store = MemorySkillStore()
    with pytest.raises(InferenceFailed) as e:
        run("basement", store=store, responses={"basement": response})
    assert e.value.code == GROUNDING_FAILED
    assert len(e.value.details["dropped_skills"]) == 3 and e.value.details["completion_hash"]
    assert store.inferences == []
    assert store.permits[fx["permit"]["id"]]["processing_state"] == "SKILLS_FAILED"


def test_zero_signal_admin_text_fails_gracefully_and_is_logged(caplog):
    fx = load("admin_only")
    store = MemorySkillStore()
    with caplog.at_level(logging.WARNING, logger="pts.skills"):
        with pytest.raises(InferenceFailed) as e:
            run("admin_only", store=store)
    assert e.value.code == NO_SKILLS and not e.value.retryable
    assert store.inferences == []
    assert store.permits[fx["permit"]["id"]]["processing_state"] == "SKILLS_FAILED"
    assert any('"event": "skills.failed"' in r.message and NO_SKILLS in r.message for r in caplog.records)


@pytest.mark.parametrize("name", NEGATIVE)
def test_negative_fixture_outcomes(name):
    fx = load(name)
    provider = fixture_provider(name)
    if fx["expected"]["outcome"] == "accepted":
        _, inf, _, _ = run(name, provider=provider)
        assert len(inf.dropped_skills) == fx["expected"]["dropped"]
    else:
        with pytest.raises(InferenceFailed) as e:
            run(name, provider=provider)
        assert e.value.code == fx["expected"]["outcome"]
        assert len(provider.calls) == fx["expected"]["llm_calls"]


# ---- AC3: idempotency, versioned rows ---------------------------------------------

def test_second_call_is_a_cache_hit():
    fx = load("basement")
    store, provider = MemorySkillStore(), fixture_provider("basement")
    _, first, _, _ = run("basement", store=store, provider=provider)
    _, second, _, _ = run("basement", store=store, provider=provider)
    assert len(provider.calls) == 1 and len(store.inferences) == 1
    assert second.cached and not first.cached
    assert second.context_hash == first.context_hash and second.inference_id == first.inference_id
    assert second.skills == first.skills
    # context is deterministic
    ctx = lambda: build_context(permit_input(fx), detail_document(fx))  # noqa: E731
    assert ctx().context_hash == ctx().context_hash == first.context_hash


def test_new_model_or_prompt_version_appends_a_row():
    store = MemorySkillStore()
    run("basement", store=store)
    other = fixture_provider("basement")
    other.default_model = "another-model"
    _, inf, _, _ = run("basement", store=store, provider=other)
    assert len(store.inferences) == 2 and not inf.cached
    assert store.inferences[0]["model"] != store.inferences[1]["model"]
    assert store.latest_inference(1)["id"] == inf.inference_id


def test_a_new_snapshot_is_a_new_cache_key():
    fx = load("basement")
    store, provider = MemorySkillStore(), fixture_provider("basement")
    run("basement", store=store, provider=provider)
    detail = detail_document(fx, snapshot_id=999)
    _, inf, _, _ = run("basement", store=store, provider=provider, detail=detail)
    assert len(provider.calls) == 2 and len(store.inferences) == 2 and inf.input_snapshot_id == 999


# ---- gate, degraded mode ----------------------------------------------------------

def test_not_eligible_status_makes_no_call_and_no_state_change():
    provider = fixture_provider("basement")
    store = MemorySkillStore()
    with pytest.raises(NotEligible):
        run("basement", provider=provider, store=store, permit_overrides={"status": "PENDING"})
    assert provider.calls == [] and store.permits[1]["processing_state"] == "DETAIL_READY"


def test_completed_allowed_when_requested():
    provider = SequenceProvider([load("basement")["response"]])
    _, inf, _, _ = run("basement", provider=provider, permit_overrides={"status": "COMPLETED"},
                       allowed_statuses=("ISSUED", "COMPLETED"))
    assert inf.skills


def test_missing_detail_needs_allow_degraded():
    with pytest.raises(NotEligible) as e:
        run("basement", detail=None)
    assert "pts detail fetch" in e.value.reason


def test_degraded_mode_cites_the_catalog_text():
    fx = load("basement")
    permit = permit_input(fx)
    url = fx["permit"]["detail_url"]
    response = {"skills": [{"skill": "Interior partition framing for basement alterations", "trade": "Carpentry",
                            "evidence": fx["permit"]["description"], "evidence_source": url,
                            "confidence": "medium"}],
                "assumptions": [], "overall_confidence": "medium", "licensing_notes": []}
    store = MemorySkillStore()
    store.add_fixture(fx, with_snapshot=False)
    provider = SequenceProvider([response])
    inf = infer_skills(permit, None, provider=provider, store=store, allow_degraded=True, settings=SETTINGS)
    assert inf.degraded and inf.overall_confidence == "low"
    assert inf.assumptions[0] == DEGRADED_ASSUMPTION
    assert inf.skills[0]["evidence_source"] == url and inf.input_snapshot_id is None
    row = store.inferences[0]
    assert row["degraded"] and row["substrate_text"] == catalog_text(permit)
    assert G.verify_row(row["skills"], row["substrate_text"], url)[0]["reasons"] == []


def test_degraded_mode_needs_a_detail_url():
    with pytest.raises(NotEligible):
        run("basement", detail=None, allow_degraded=True, permit_overrides={"detail_url": None})


def test_degraded_snapshot_caps_confidence():
    fx = load("tenant_improvement")
    detail = detail_document(fx, degraded=True, notes=["refresh failed (TIMEOUT)"])
    _, inf, _, _ = run("tenant_improvement", detail=detail,
                       provider=SequenceProvider([fx["response"]]))
    assert inf.overall_confidence == "medium"
    assert any("marked degraded" in a and "TIMEOUT" in a for a in inf.assumptions)


# ---- validation retry, provider failure ------------------------------------------

def test_schema_retry_then_success():
    fx = load("basement")
    broken = copy.deepcopy(fx["response"])
    del broken["skills"][0]["trade"]                     # dropped field
    provider = SequenceProvider([broken, fx["response"]])
    _, inf, _, _ = run("basement", provider=provider)
    assert len(provider.calls) == 2 and len(inf.skills) == 3
    assert "Your last output was invalid" in provider.calls[1]["user"]
    assert "Your last output was invalid" not in provider.calls[0]["user"]


def test_pydantic_only_violation_is_retried():
    fx = load("basement")
    short = copy.deepcopy(fx["response"])
    short["skills"][0]["skill"] = "Walls"                # minLength 8, not in the provider schema
    provider = SequenceProvider([short, fx["response"]])
    run("basement", provider=provider)
    assert len(provider.calls) == 2 and "skills.0.skill" in provider.calls[1]["user"]


def test_schema_invalid_twice_fails_retryable():
    fx = load("basement")
    broken = copy.deepcopy(fx["response"])
    del broken["overall_confidence"]
    store = MemorySkillStore()
    with pytest.raises(InferenceFailed) as e:
        run("basement", provider=SequenceProvider([broken, broken]), store=store)
    assert e.value.code == SCHEMA_INVALID and e.value.retryable
    assert store.permits[1]["processing_state"] == "SKILLS_FAILED"


def test_provider_failure_never_fabricates():
    store = MemorySkillStore()
    with pytest.raises(InferenceFailed) as e:
        run("basement", provider=SequenceProvider([LLMUnavailable("ollama down")]), store=store)
    assert e.value.code == PROVIDER_UNAVAILABLE and e.value.retryable
    assert store.inferences == [] and store.state_errors[1]["retryable"] is True


def test_failure_after_success_keeps_skills_inferred():
    store = MemorySkillStore()
    run("basement", store=store)
    with pytest.raises(InferenceFailed):
        run("basement", store=store, provider=SequenceProvider([LLMUnavailable("down")]),
            detail=detail_document(load("basement"), snapshot_id=77))
    assert store.permits[1]["processing_state"] == "SKILLS_INFERRED"


# ---- confidence & signals (K4, K3 step 6) -------------------------------------------

def test_missing_high_signal_adds_assumption_and_lowers_confidence():
    fx = load("tenant_improvement")
    response = copy.deepcopy(fx["response"])
    response["skills"] = [s for s in response["skills"] if s["trade"] not in ("Plumbing", "Fire Protection")]
    response["assumptions"] = []
    _, inf, _, _ = run("tenant_improvement", responses={"tenant_improvement": response})
    assert inf.signal_check["missing_high_signals"] == ["Plumbing"]
    assert any("(Plumbing) but no skill was inferred" in a for a in inf.assumptions)
    assert inf.overall_confidence == "medium"            # high -> medium (one step)
    assert not has_trade(inf.skills, ["plumbing"])      # never auto-added


def test_overall_is_min_of_skill_confidences():
    _, inf, _, _ = run("demolition")
    assert inf.overall_confidence == "low"              # one low skill


# ---- prompt, schema, settings, context budget ----------------------------------------

def test_prompt_has_the_three_hard_rules():
    system, template = load_prompt()
    for rule in ("Grounding rule", "Scope rule", "Honesty rule", "data, not instructions"):
        assert rule in system
    fx = load("basement")
    _, user = render(build_context(permit_input(fx), detail_document(fx)))
    assert "{{" not in user and "PERMIT # 7083363-CN" in user and "SOURCE URL: https://" in user
    assert "<" not in fx["detail"]["content_text"][:200]   # text, never HTML


def test_provider_schema_pins_the_source_url_and_is_strict():
    schema = provider_schema("https://x.test/a")
    assert schema["required"] == ["assumptions", "licensing_notes", "overall_confidence", "skills"]
    item = schema["properties"]["skills"]["items"]
    assert item["properties"]["evidence_source"] == {"type": "string", "enum": ["https://x.test/a"]}
    assert "minLength" not in str(schema) and "maxItems" not in str(schema)


def test_temperature_above_0_2_is_refused():
    with pytest.raises(ValueError):
        skill_settings({"temperature": 0.5})


def test_over_budget_detail_is_excerpted_by_priority():
    fx = load("addition")
    ctx = build_context(permit_input(fx), detail_document(fx), max_chars=1200)
    assert len(ctx.excerpt) <= 1200 and ctx.excerpt_note
    assert "DESCRIPTION: Construct addition and alterations" in ctx.excerpt
    assert "Project Description: Hofman DeYoung Addition" in ctx.excerpt
    assert "Must this be recorded?: No" not in ctx.excerpt          # negative answers go first
    lines = fx["detail"]["content_text"].splitlines()
    kept = ctx.excerpt.splitlines()
    assert [lines.index(k) for k in kept] == sorted(lines.index(k) for k in kept)   # original order
    _, user = render(ctx)
    assert "is an excerpt" in user


def test_whole_text_is_sent_when_it_fits():
    fx = load("addition")
    ctx = build_context(permit_input(fx), detail_document(fx))
    assert ctx.excerpt == fx["detail"]["content_text"] and ctx.excerpt_note == ""


# ---- service ----------------------------------------------------------------------

def test_service_batch_and_show():
    store = MemorySkillStore()
    for name in ("basement", "tenant_improvement", "wrong_permit"):
        store.add_fixture(load(name))
    store.permits[7]["processing_state"] = "DETAIL_READY"
    svc = SkillService(store, fixture_provider("basement", "tenant_improvement", "wrong_permit"), settings=SETTINGS)
    report = svc.batch("seattle-kingcounty-wa", state="DETAIL_READY", limit=10)
    assert (report["inferred"], report["failed"], report["total"]) == (2, 1, 3)
    shown = svc.show("7190001-CN", "seattle-kingcounty-wa")
    assert all(t["grounded"] for t in shown["grounding"]) and len(shown["grounding"]) == 8
    assert shown["contract"]["permit_key"] == "seattle-kingcounty-wa:7190001-CN"
    again = svc.batch("seattle-kingcounty-wa", state=None, limit=10)
    assert again["cached"] == 2

"""NLU guardrails (Stage 04 Q2): fixture LLM responses, exact error codes and
unfulfilled_filters behavior. No database or real LLM."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from permit_trade_skills.server import create_app
from permit_trade_skills.llm import LLMResponseInvalid, LLMUnavailable
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.query.echo import render_echo
from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.nlu import MemoryNluCache, cache_key, match_jurisdictions
from permit_trade_skills.settings import DEFAULTS
from permit_trade_skills.store.query import PermitQuery
from query_fakes import SEATTLE, TACOMA, StubService, fake_row, nlu_payload

SLUG = SEATTLE["slug"]


def ask(service, question="q", jurisdiction=None):
    return service.ask(question, jurisdiction)


def error_of(fn) -> QueryError:
    with pytest.raises(QueryError) as info:
        fn()
    return info.value


# ---- jurisdiction ---------------------------------------------------------------

def test_unregistered_jurisdiction_named_in_question_is_422_with_registered_list():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"], jurisdiction="Bellevue")),
                      jurisdictions=[SEATTLE, TACOMA])
    e = error_of(lambda: ask(svc, "issued permits in Bellevue"))
    assert (e.error_code, e.status) == ("JURISDICTION_NOT_REGISTERED", 422)
    assert e.extra["registered_jurisdictions"] == [SLUG, TACOMA["slug"]]
    assert svc.executed == []  # never silently queried another jurisdiction


def test_no_jurisdiction_with_two_registered_is_422_required():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"])), jurisdictions=[SEATTLE, TACOMA])
    e = error_of(lambda: ask(svc, "issued permits"))
    assert (e.error_code, e.status) == ("JURISDICTION_REQUIRED", 422)
    assert svc.executed == []


def test_no_jurisdiction_with_one_registered_defaults_and_says_so():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"])))
    out = ask(svc, "issued permits")
    assert out["jurisdiction"] == SLUG
    assert any("only registered jurisdiction" in a for a in out["assumptions"])


def test_named_jurisdiction_resolves_among_several():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"], jurisdiction="Tacoma, WA")),
                      jurisdictions=[SEATTLE, TACOMA])
    assert ask(svc, "issued permits in Tacoma, WA")["jurisdiction"] == TACOMA["slug"]


def test_question_conflicting_with_request_jurisdiction_is_422():
    svc = StubService(FakeProvider(nlu_payload(jurisdiction="Tacoma")), jurisdictions=[SEATTLE, TACOMA])
    e = error_of(lambda: ask(svc, "permits in Tacoma", jurisdiction=SLUG))
    assert e.error_code == "JURISDICTION_CONFLICT"
    assert svc.executed == []


def test_unknown_request_jurisdiction_is_404():
    svc = StubService(FakeProvider(nlu_payload()))
    e = error_of(lambda: ask(svc, "permits", jurisdiction="nowhere-xx"))
    assert (e.error_code, e.status) == ("JURISDICTION_NOT_FOUND", 404)


@pytest.mark.parametrize("named,expected", [
    ("Seattle", [SLUG]), ("City of Seattle", [SLUG]), ("Seattle, WA", [SLUG]),
    ("King County", [SLUG]), ("Bellevue", []), ("Seattle, OR", []), ("", []),
])
def test_match_jurisdictions(named, expected):
    assert match_jurisdictions(named, [SEATTLE, TACOMA]) == expected


# ---- ambiguity, unfulfilled filters, clarification -------------------------------

def test_ambiguous_term_is_reported_and_choice_applied():
    svc = StubService(FakeProvider(nlu_payload(
        permit_class="Residential", ambiguous=["residential: interpreted as permit_class=Residential"])),
        rows=[fake_row(1)])
    out = ask(svc, "residential permits")
    assert out["ambiguous_terms"] == ["residential: interpreted as permit_class=Residential"]
    assert out["query"]["permit_class"] == ["Residential"]


def test_geo_entity_is_listed_verbatim_and_rest_of_query_still_runs():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"], unrecognized=["near the stadium"])),
                      rows=[fake_row(1)])
    out = ask(svc, "issued permits near the stadium")
    assert out["unfulfilled_filters"] == ["near the stadium"]
    assert out["executed"] is True and out["count"] == 1
    assert out["clarification"] is None   # rows were found, no clarification


def test_unfulfillable_core_requirement_returns_empty_rows_and_labeled_clarification():
    clarification = "I can't search by distance; try a ZIP code such as 98109."
    svc = StubService(FakeProvider(nlu_payload(
        unrecognized=["within 1 mile of the space needle"], unfulfillable=True,
        clarification=clarification)), rows=[fake_row(1)])
    out = ask(svc, "permits within 1 mile of the space needle")
    assert out["executed"] is False and out["rows"] == [] and out["count"] == 0
    assert out["unfulfilled_filters"] == ["within 1 mile of the space needle"]
    assert out["clarification"] == {"text": clarification, "llm_generated": True}
    assert svc.executed == []   # no fabricated answer


def test_empty_result_with_unfulfilled_filter_gets_template_clarification():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"], unrecognized=["by ACME Builders"])))
    out = ask(svc, "issued permits by ACME Builders")
    assert out["count"] == 0
    assert out["clarification"]["llm_generated"] is False
    assert "by ACME Builders" in out["clarification"]["text"]


def test_out_of_schema_values_go_to_unfulfilled_not_silently_dropped():
    svc = StubService(FakeProvider(nlu_payload(issued_after="last spring", min_cost=-5)), rows=[fake_row(1)])
    out = ask(svc, "permits since last spring")
    assert len(out["unfulfilled_filters"]) == 2
    assert any(f.startswith("issued_after='last spring'") for f in out["unfulfilled_filters"])
    assert "issued_after" not in out["query"] and "min_cost" not in out["query"]


# ---- NLU failure --------------------------------------------------------------------

@pytest.mark.parametrize("exc", [LLMUnavailable("down"), LLMResponseInvalid("not json")])
def test_nlu_failure_is_503_retryable_and_never_guesses(exc):
    svc = StubService(FakeProvider(exc))
    e = error_of(lambda: ask(svc, "issued permits"))
    assert (e.error_code, e.status, e.retryable) == ("NLU_UNAVAILABLE", 503, True)
    assert svc.executed == []


def test_nlu_failure_over_http_is_problem_json_503():
    svc = StubService(FakeProvider(LLMUnavailable("down")))
    settings = {**DEFAULTS, "api": {"rate_limit_per_minute": 0}}
    client = TestClient(create_app(service=svc, settings=settings))
    resp = client.post("/api/v1/query", json={"question": "issued permits"}, headers={"X-Trace-Id": "t-1"})
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["error_code"] == "NLU_UNAVAILABLE" and body["retryable"] is True
    assert body["trace_id"] == "t-1" == resp.headers["X-Trace-Id"]


# ---- cache --------------------------------------------------------------------------

def test_cache_hit_skips_the_llm():
    provider = FakeProvider(nlu_payload(status=["ISSUED"]))
    cache = MemoryNluCache()
    svc = StubService(provider, cache=cache)
    first = ask(svc, "issued permits")
    second = ask(svc, "  issued   permits ")   # whitespace-normalized key
    assert len(provider.calls) == 1
    assert (first["nlu"]["cached"], second["nlu"]["cached"]) == (False, True)
    assert first["query"] == second["query"]
    assert len(cache.entries) == 1


def test_cache_key_varies_with_jurisdiction_model_and_day():
    from datetime import date

    base = cache_key("q", None, "m", date(2026, 11, 6))
    assert base != cache_key("q", SLUG, "m", date(2026, 11, 6))
    assert base != cache_key("q", None, "m2", date(2026, 11, 6))
    assert base != cache_key("q", None, "m", date(2026, 11, 7))


# ---- echo is template-rendered from the executed query (no fabrication) ---------------

def test_echo_matches_executed_query_when_nlu_drops_a_filter():
    # The question asks for ISSUED permits, but the NLU "forgets" status.
    svc = StubService(FakeProvider(nlu_payload(permit_class="Residential", issued_after="2026-08-08",
                                               min_cost=50000)), rows=[fake_row(1, "COMPLETED")])
    out = ask(svc, "issued residential permits over $50k in the last 90 days")
    executed = svc.executed[0]
    assert "status" not in out["query"]
    assert out["echo"] == render_echo(executed, suffix="(interpreted from your question)")
    assert "ISSUED" not in out["echo"]
    assert out["echo"].startswith("Showing permits, Residential class, issued on or after 2026-08-08, "
                                  "est. cost >= $50,000")


def test_echo_template():
    q = PermitQuery(jurisdiction=SLUG, status=["ISSUED"], permit_class="Residential",
                    issued_after=datetime(2026, 6, 28), min_cost=50000, desc_keyword="ADU",
                    statuses_exclude=["CANCELED"], zip="98122")
    assert render_echo(q) == (
        "Showing ISSUED permits, excluding CANCELED, Residential class, description contains \"ADU\", "
        "ZIP 98122, issued on or after 2026-06-28, est. cost >= $50,000 in seattle-kingcounty-wa, "
        "sorted by issue date (newest first).")


# ---- deterministic guards on the parse ---------------------------------------------

def test_city_that_is_the_jurisdiction_is_not_an_address_filter():
    svc = StubService(FakeProvider(nlu_payload(status=["PENDING"], city="Seattle", jurisdiction="Seattle")))
    out = ask(svc, "pending permits in Seattle")
    assert "city" not in out["query"]
    assert any("not applied as an address-city filter" in a for a in out["assumptions"])


def test_other_city_is_kept_as_a_filter():
    svc = StubService(FakeProvider(nlu_payload(city="Shoreline")))
    assert ask(svc, "permits with a Shoreline address")["query"]["city"] == "Shoreline"


def test_class_and_type_are_restricted_to_known_values():
    from permit_trade_skills.query.nlu import nlu_schema

    schema = nlu_schema({"permit_type": ["Building", "Roof"]})
    assert schema["properties"]["permit_query"]["properties"]["permit_type"]["enum"] == ["Building", "Roof", None]
    # A parse with an invented permit_type fails schema validation -> 503, never executed.
    svc = StubService(FakeProvider(nlu_payload(permit_type="Solar")))
    e = error_of(lambda: ask(svc, "solar permits"))
    assert e.error_code == "NLU_UNAVAILABLE" and svc.executed == []


# ---- "top N" -----------------------------------------------------------------------

def test_top_n_limits_the_page_and_is_echoed():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"], min_cost=100000, sort="cost_desc", limit=5)))
    out = ask(svc, "show the top 5 issued permits with estimated cost of 100000 or more")
    assert out["query"]["page_size"] == 5 and out["query"]["min_cost"] == 100000.0
    assert out["echo"].startswith("Showing the first 5 ISSUED permits, est. cost >= $100,000")


def test_explicit_page_size_overrides_parsed_limit_and_large_limits_are_clamped():
    svc = StubService(FakeProvider(nlu_payload(limit=5)))
    assert svc.ask("top 5 permits", page_size=10)["query"]["page_size"] == 10
    svc = StubService(FakeProvider(nlu_payload(limit=1000)))
    assert ask(svc, "top 1000 permits")["query"]["page_size"] == 200


def test_default_page_size_is_not_echoed():
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"])))
    assert ask(svc, "issued permits")["echo"].startswith("Showing ISSUED permits")


# ---- trade filter -----------------------------------------------------------------

def test_trade_parse_runs_as_trade_filter_not_keyword():
    svc = StubService(FakeProvider(nlu_payload(trade=["Carpentry"], issued_after="2026-10-14", limit=10)))
    out = ask(svc, "top 10 permits requiring carpentry work in the last 30 days")
    q = svc.executed[0]
    assert q.trade == ["Carpentry"] and q.desc_keyword is None and q.page_size == 10
    assert q.trade_min_confidence == "medium" and q.trade_match is None   # defaults
    assert any("medium or higher" in a for a in out["assumptions"])
    assert "needing Carpentry work per inferred trade skills (confidence medium or higher)" in out["echo"]


def test_several_trades_default_to_all_and_explicit_values_are_kept():
    svc = StubService(FakeProvider(nlu_payload(trade=["Electrical", "Carpentry"])))
    out = ask(svc, "electrical carpentry permits")
    q = svc.executed[0]
    assert q.trade_match == "all" and any("all of Electrical, Carpentry" in a for a in out["assumptions"])
    assert "needing Electrical and Carpentry work" in out["echo"]

    svc = StubService(FakeProvider(nlu_payload(trade=["Plumbing", "HVAC"], trade_match="any",
                                               trade_min_confidence="low")))
    out = ask(svc, "permits that might need plumbing or HVAC work")
    q = svc.executed[0]
    assert (q.trade_match, q.trade_min_confidence) == ("any", "low")
    assert not any("medium or higher" in a for a in out["assumptions"])
    assert "needing Plumbing or HVAC work" in out["echo"]


def test_trade_modifiers_without_a_trade_are_dropped():
    svc = StubService(FakeProvider(nlu_payload(desc_keyword="deck", trade_match="all",
                                               trade_min_confidence="high")))
    ask(svc, "deck permits")
    q = svc.executed[0]
    assert q.trade is None and q.trade_match is None and q.trade_min_confidence is None


def test_nlu_schema_restricts_trade_to_known_values():
    from permit_trade_skills.query.nlu import nlu_schema

    pq = nlu_schema({"trade": ["Carpentry", "Electrical"]})["properties"]["permit_query"]
    assert pq["properties"]["trade"]["items"]["enum"] == ["Carpentry", "Electrical"]
    assert "trade" in pq["required"]
    assert "enum" not in nlu_schema({})["properties"]["permit_query"]["properties"]["trade"]["items"]


def test_echo_trade_with_confidence():
    q = PermitQuery(jurisdiction=SLUG, trade=["Plumbing", "Electrical"], trade_min_confidence="medium")
    assert render_echo(q) == (
        "Showing permits, needing Plumbing or Electrical work per inferred trade skills (confidence "
        "medium or higher) in seattle-kingcounty-wa, sorted by issue date (newest first).")


def test_trade_coverage_note_only_when_permits_lack_inference():
    from permit_trade_skills.query.service import trade_coverage

    assert trade_coverage(10, 10) == {"candidates": 10, "inferred": 10, "not_inferred": 0, "note": None}
    gap = trade_coverage(470, 31)
    assert gap["not_inferred"] == 439 and gap["note"].startswith("439 of 470 permits")

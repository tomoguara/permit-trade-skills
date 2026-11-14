"""Parameter validation shared by REST, tools and NLU; rate limiter; API error shapes."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from permit_trade_skills.server import create_app
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.query.api import RateLimiter
from permit_trade_skills.query.errors import QueryError
from permit_trade_skills.query.nlu import SCHEMA_VERSION
from permit_trade_skills.query.params import parse_params
from permit_trade_skills.settings import DEFAULTS, _merge
from permit_trade_skills.store.query import PermitSort
from query_fakes import StubService, nlu_payload

J = "seattle-kingcounty-wa"


def test_csv_lists_dates_and_numbers():
    q, problems = parse_params(J, {"status": "issued, pending", "permit_class": "Residential",
                                   "issued_after": "2026-08-08", "issued_before": "2026-09-01T12:00:00Z",
                                   "min_cost": "50000", "zip": " 98122 ", "sort": "cost_desc",
                                   "page_size": 1000, "city": None})
    assert problems == []
    assert q.status == ["ISSUED", "PENDING"] and q.permit_class == ["Residential"]
    assert q.issued_after == datetime(2026, 8, 8) and q.issued_before == datetime(2026, 9, 1, 12)
    assert q.min_cost == 50000.0 and q.zip == "98122" and q.sort is PermitSort.COST_DESC
    assert q.page_size == 200   # clamped


def test_trade_params():
    q, problems = parse_params(J, {"trade": "Carpentry, electrical", "trade_min_confidence": "medium",
                                   "trade_match": "all"})
    assert problems == [] and q.trade == ["Carpentry", "electrical"] and q.trade_min_confidence == "medium"
    assert q.trade_match == "all"
    assert q.to_dict()["trade"] == ["Carpentry", "electrical"]


@pytest.mark.parametrize("params,code", [
    ({"status": "CLOSED"}, "INVALID_PARAMETER"),          # no CLOSED status in the canonical enum
    ({"trade_min_confidence": "very"}, "INVALID_PARAMETER"),
    ({"trade_match": "both"}, "INVALID_PARAMETER"),
    ({"state": "DONE"}, "INVALID_PARAMETER"),
    ({"sort": "issued_desc"}, "INVALID_PARAMETER"),
    ({"issued_after": "yesterday"}, "INVALID_PARAMETER"),
    ({"min_cost": "lots"}, "INVALID_PARAMETER"),
    ({"min_cost": 10, "max_cost": 5}, "INVALID_PARAMETER"),
    ({"issued_after": "2026-09-01", "issued_before": "2026-09-01"}, "INVALID_DATE_RANGE"),
    ({"q": "roof"}, "INVALID_PARAMETER"),
    ({"page": 2, "cursor": "abc"}, "INVALID_PARAMETER"),
])
def test_strict_rejects(params, code):
    with pytest.raises(QueryError) as info:
        parse_params(J, params)
    assert info.value.error_code == code and info.value.status == 422


def test_lenient_collects_problems_and_keeps_the_rest():
    q, problems = parse_params(J, {"status": ["ISSUED"], "issued_after": "last spring",
                                   "applied_after": "2026-05-01", "applied_before": "2026-01-01"},
                               strict=False)
    assert q.status == ["ISSUED"] and q.issued_after is None and q.applied_after is None
    assert len(problems) == 2


def test_rate_limiter_fixed_window():
    limiter = RateLimiter(2)
    assert limiter.hit("ip", now=60.0)[:2] == (True, 1)
    assert limiter.hit("ip", now=61.0)[:2] == (True, 0)
    allowed, remaining, reset = limiter.hit("ip", now=62.0)
    assert (allowed, remaining, reset) == (False, 0, 120)
    assert limiter.hit("other", now=62.0)[0] is True
    assert limiter.hit("ip", now=120.0)[0] is True   # next window


def client(limit=0):
    svc = StubService(FakeProvider(nlu_payload(status=["ISSUED"])))
    return TestClient(create_app(service=svc, settings=_merge(DEFAULTS, {"api": {"rate_limit_per_minute": limit}})))


def test_rate_limit_headers_and_429_problem():
    c = client(limit=2)
    first = c.post("/api/v1/query", json={"question": "issued permits"})
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2" and first.headers["X-RateLimit-Remaining"] == "1"
    c.post("/api/v1/query", json={"question": "issued permits"})
    third = c.post("/api/v1/query", json={"question": "issued permits"})
    assert third.status_code == 429 and third.json()["error_code"] == "RATE_LIMITED"
    assert third.headers["Retry-After"] and third.headers["X-Trace-Id"]
    assert c.get("/health").status_code == 200   # not rate limited


def test_request_validation_is_problem_json():
    resp = client().post("/api/v1/query", json={"question": ""})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["error_code"] == "INVALID_PARAMETER"


def test_query_response_shape_and_trace_id():
    resp = client().post("/api/v1/query", json={"question": "issued permits"}, headers={"X-Trace-Id": "abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == "abc" == resp.headers["X-Trace-Id"]
    assert body["echo"].startswith("Showing ISSUED permits")
    assert body["query"]["status"] == ["ISSUED"] and body["executed"] is True
    assert body["nlu"]["schema_version"] == SCHEMA_VERSION


def test_openapi_documents_the_frozen_shapes():
    spec = client().get("/openapi.json").json()
    assert set(spec["paths"]) >= {"/api/v1/jurisdictions", "/api/v1/permits", "/api/v1/permits/{permit_number}",
                                  "/api/v1/status-counts", "/api/v1/query", "/health"}
    rows = spec["components"]["schemas"]["PermitsResponse"]["properties"]
    assert set(rows) >= {"jurisdiction", "query", "total_estimate", "count", "next_cursor", "rows"}

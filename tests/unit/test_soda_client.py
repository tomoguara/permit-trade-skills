"""SodaClient over a fake httpx transport: retries, typed errors, row parsing (B4, B5, B11)."""

import asyncio

import httpx
import pytest

from permit_trade_skills.ingest import soda_client as sc
from permit_trade_skills.ingest.soda_client import SodaClient, SourceUnavailable, soda_resource_base

BASE = "https://data.example.gov/resource/abcd-1234"


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff waits instead of sleeping."""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)
    return waits


def run(handler, coro_fn, **kw):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request, len(calls))

    async def go():
        async with SodaClient(BASE, transport=httpx.MockTransport(wrapped), **kw) as client:
            return await coro_fn(client)

    return asyncio.run(go()), calls


def test_resource_base_builds_row_endpoint():
    assert soda_resource_base("https://cos-data.seattle.gov/", "76t5-zqzr") == \
        "https://cos-data.seattle.gov/resource/76t5-zqzr"
    assert soda_resource_base("", "76t5-zqzr") is None
    assert soda_resource_base("https://x.gov", None) is None


def test_list_rows_sends_paging_and_caller_order(sleeps):
    rows, calls = run(lambda req, n: httpx.Response(200, json=[{"permitnum": "A"}]),
                      lambda c: c.list_rows({"$where": "x > 1"}, page=3, limit=500, order="permitnum ASC"))
    assert rows == [{"permitnum": "A"}]
    params = calls[0].url.params
    assert calls[0].url.path == "/resource/abcd-1234.json"
    assert (params["$limit"], params["$offset"], params["$order"], params["$where"]) == \
        ("500", "1000", "permitnum ASC", "x > 1")


def test_list_rows_has_no_hardcoded_order(sleeps):
    _, calls = run(lambda req, n: httpx.Response(200, json=[]), lambda c: c.list_rows({}))
    assert "$order" not in calls[0].url.params


def test_5xx_is_retried_then_succeeds(sleeps):
    def handler(req, n):
        return httpx.Response(503) if n == 1 else httpx.Response(200, json=[{"a": 1}])

    rows, calls = run(handler, lambda c: c.list_rows({}))
    assert rows == [{"a": 1}] and len(calls) == 2
    assert sleeps == [1.0]


def test_429_honours_retry_after(sleeps):
    def handler(req, n):
        if n == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=[])

    run(handler, lambda c: c.list_rows({}))
    assert sleeps == [7.0]


def test_retry_after_is_capped(sleeps):
    def handler(req, n):
        return httpx.Response(429, headers={"Retry-After": "3600"}) if n == 1 else httpx.Response(200, json=[])

    run(handler, lambda c: c.list_rows({}))
    assert sleeps == [SodaClient.MAX_RETRY_AFTER]


def test_persistent_5xx_raises_retryable_source_unavailable(sleeps):
    with pytest.raises(SourceUnavailable) as err:
        run(lambda req, n: httpx.Response(500), lambda c: c.list_rows({}))
    assert err.value.retryable is True and err.value.status_code == 500
    assert sleeps == [1.0, 2.0, 4.0]  # MAX_RETRIES retries after the first attempt


def test_4xx_fails_at_once_not_retryable(sleeps):
    calls = []

    def handler(req, n):
        calls.append(n)
        return httpx.Response(404, text="no such dataset")

    with pytest.raises(SourceUnavailable) as err:
        run(handler, lambda c: c.list_rows({}))
    assert err.value.retryable is False and err.value.status_code == 404
    assert calls == [1] and sleeps == []


def test_network_error_retried_then_typed(sleeps):
    def handler(req, n):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(SourceUnavailable) as err:
        run(handler, lambda c: c.list_rows({}))
    assert err.value.retryable is True and "ConnectError" in str(err.value)


def test_non_json_body_is_not_retryable(sleeps):
    with pytest.raises(SourceUnavailable) as err:
        run(lambda req, n: httpx.Response(200, text="<html>"), lambda c: c.list_rows({}))
    assert err.value.retryable is False


def test_distinct_values_parses_object_rows(sleeps):
    body = [{"statuscurrent": "Issued"}, {"statuscurrent": "Completed"}, {}]
    values, calls = run(lambda req, n: httpx.Response(200, json=body),
                        lambda c: c.distinct_values("statuscurrent"))
    assert values == ["Issued", "Completed"]
    assert calls[0].url.params["$group"] == "statuscurrent"


def test_count_parses_object_row(sleeps):
    total, _ = run(lambda req, n: httpx.Response(200, json=[{"count": "193249"}]), lambda c: c.count())
    assert total == 193249


def test_get_row_by_key_escapes_quotes(sleeps):
    row, calls = run(lambda req, n: httpx.Response(200, json=[{"permitnum": "O'X"}]),
                     lambda c: c.get_row_by_key("permitnum", "O'X"))
    assert row == {"permitnum": "O'X"}
    assert calls[0].url.params["$where"] == "permitnum='O''X'"

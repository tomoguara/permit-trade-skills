"""Stage 01 offline tests: platform probes, catalog client, config validation (fake httpx transport)."""

import asyncio
import logging

import httpx
import pytest

from permit_trade_skills.cli.seeds import SEATTLE_FIELD_MAP, SEATTLE_STATUS_MAP
from permit_trade_skills.discovery import catalog_client as cc
from permit_trade_skills.discovery.catalog_client import CatalogClient, candidate_from_package
from permit_trade_skills.discovery.schemas import DiscoveryStatus, PlatformType
from permit_trade_skills.discovery.validate import validate_config
from permit_trade_skills.discovery.verifier import Verifier
from permit_trade_skills.ingest.soda_client import SourceUnavailable, fetch_columns

SEATTLE_STRATEGY = {"timestamp_field": "issueddate",
                    "detail_url_rule": "https://x.gov/r?altId={{permitnum}}"}


def verify(url, routes):
    """Run Verifier.verify against a fake transport: routes maps URL path -> Response."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return routes.get(request.url.path, httpx.Response(404))

    async def go():
        async with Verifier(transport=httpx.MockTransport(handler)) as v:
            return await v.verify(url)

    return asyncio.run(go()), seen


# ---- Verifier -------------------------------------------------------------

def test_socrata_api_check():
    result, _ = verify("https://data.city.gov/d/abcd-1234",
                       {"/api/views.json": httpx.Response(200, json={"meta": {}})})
    assert (result.detected_platform, result.probe_name, result.confidence) == \
        (PlatformType.SOCRATA, "socrata_api_check", 0.95)


def test_gov_host_falls_back_to_domain_check():
    result, seen = verify("https://data.city.gov/d/abcd-1234", {})
    assert (result.detected_platform, result.probe_name, result.confidence) == \
        (PlatformType.SOCRATA, "socrata_domain_check", 0.75)
    assert seen == ["/api/views.json", "/api/views/4000.json"]


def test_ckan_probe():
    result, _ = verify("https://opendata.example.org/dataset/permits",
                       {"/api/3/action/package_list": httpx.Response(200, json={"result": ["a", "b"]})})
    assert (result.detected_platform, result.probe_name) == (PlatformType.CKAN, "ckan_api_check")


def test_generic_api_probe():
    result, _ = verify("https://permits.example.org/x", {"/data.json": httpx.Response(200, json={})})
    assert (result.detected_platform, result.confidence) == (PlatformType.OTHER, 0.60)


def test_nothing_detected_is_fallback_other():
    result, seen = verify("https://permits.example.org/x", {})
    assert (result.probe_name, result.detected_platform, result.confidence) == \
        ("fallback", PlatformType.OTHER, 0.0)
    assert len(seen) == 7  # 3 CKAN + 4 generic endpoints


def test_network_errors_do_not_abort():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    async def go():
        async with Verifier(transport=httpx.MockTransport(handler)) as v:
            return await v.verify("https://data.city.gov/d/x")

    result = asyncio.run(go())
    assert result.detected_platform == PlatformType.SOCRATA  # .gov domain fallback


# ---- CatalogClient --------------------------------------------------------

PACKAGE = {
    "id": "pkg-1", "title": "Building Permits", "url": "https://data.city.gov/d/abcd-1234",
    "notes": "All permits", "organization": {"title": "City of X"},
    "extras": [{"key": "identifier", "value": "https://data.city.gov/api/views/abcd-1234"},
               {"key": "publisher", "value": "data.city.gov"}],
    "resources": [{"format": "CSV", "url": "https://data.city.gov/rows.csv"},
                  {"format": "JSON", "url": "https://data.city.gov/rows.json", "describedBy": "https://d/c.json"}],
}


@pytest.fixture
def sleeps(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(cc.asyncio, "sleep", fake_sleep)
    return waits


def catalog_run(handler, **kw):
    client = CatalogClient(transport=httpx.MockTransport(handler))
    return asyncio.run(client.discovery_run("X", "X", "WA", **kw))


def test_candidate_carries_dcat_fields():
    c = candidate_from_package(PACKAGE)
    assert (c.identifier, c.publisher, c.description) == \
        ("https://data.city.gov/api/views/abcd-1234", "data.city.gov", "All permits")
    assert c.distributions[1] == {"format": "JSON", "downloadURL": "https://data.city.gov/rows.json",
                                  "describedBy": "https://d/c.json"}
    assert c.platform_type is None


def test_custom_query_and_limit(sleeps):
    calls = []

    def handler(request):
        calls.append(request.url.params)
        return httpx.Response(200, json={"result": {"count": 2, "results": [PACKAGE, PACKAGE | {"id": "pkg-2"}]}})

    run, candidates = catalog_run(handler, query='"building permits" WA', limit=1)
    assert [c.id for c in candidates] == ["pkg-1"]
    assert calls[0]["q"] == '"building permits" WA' and len(calls) == 1
    assert run.search_parameters["queries"] == ['"building permits" WA']


def test_default_queries_unchanged(sleeps):
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"result": {"results": []}})

    catalog_run(handler)
    assert calls == ['"X"', "x", "x permits"]


def test_429_retry_after_then_success(sleeps, caplog):
    responses = iter([httpx.Response(429, headers={"Retry-After": "5"}),
                      httpx.Response(200, json={"result": {"count": 1, "results": [PACKAGE]}})])

    def handler(request):
        return next(responses)

    with caplog.at_level(logging.INFO, logger="permit_trade_skills.discovery.catalog_client"):
        run, candidates = catalog_run(handler, limit=1)
    assert run.status == DiscoveryStatus.COMPLETED and len(candidates) == 1
    assert sleeps == [5.0]
    logged = [r.getMessage() for r in caplog.records]
    assert any("status=429" in m for m in logged)
    assert any("status=200" in m and "returned=1" in m for m in logged)


def test_persistent_5xx_fails_the_run(sleeps):
    run, candidates = catalog_run(lambda request: httpx.Response(503))
    assert run.status == DiscoveryStatus.FAILED and "503" in run.error_message and candidates == []
    assert sleeps == [1.0, 2.0]  # 3 attempts


def test_4xx_is_not_retried(sleeps):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    run, _ = catalog_run(handler)
    assert run.status == DiscoveryStatus.FAILED and calls == [1] and sleeps == []


# ---- validate_config ------------------------------------------------------

def test_seattle_seed_is_clean():
    result = validate_config(SEATTLE_FIELD_MAP, SEATTLE_STATUS_MAP,
                             {"timestamp_field": "issueddate",
                              "detail_url_rule": "https://x/?altId={{permitnum}}"})
    assert result.ok and result.warnings == []


def test_b2_style_status_map_is_an_error():
    result = validate_config({"permit_num": "permitnum"},
                             {"approved": ["Issued"], "in_progress": ["Pending"]}, SEATTLE_STRATEGY)
    assert not result.ok
    assert sum("is not a canonical status" in e for e in result.errors) == 2


def test_missing_permit_num_and_bad_placeholder_are_errors():
    result = validate_config({"status_current": "status"}, {"issued": ["Issued"]},
                             {"detail_url_rule": "https://x/{{permitnum}}", "timestamp_field": "issued"})
    assert any("permit_num" in e for e in result.errors)
    assert any("{{permitnum}}" in e for e in result.errors)
    assert any("timestamp_field 'issued'" in w for w in result.warnings)


def test_warnings_do_not_block():
    result = validate_config({"permit_num": "p", "colour": "c"}, {}, {})
    assert result.ok
    text = " ".join(result.warnings)
    assert "'colour' is not a canonical field" in text
    assert "status_map is empty" in text and "timestamp_field is not set" in text
    assert "'status_current' is not mapped" in text


def test_duplicate_raw_status_warns():
    result = validate_config({"permit_num": "p"}, {"issued": ["Active"], "pending": ["active"]},
                             {"timestamp_field": "p"})
    assert result.ok and any("listed under both" in w for w in result.warnings)


def test_columns_check_reports_each_missing_column():
    result = validate_config(SEATTLE_FIELD_MAP, SEATTLE_STATUS_MAP, SEATTLE_STRATEGY,
                             columns=set(SEATTLE_FIELD_MAP.values()) - {"workclass", "numbuildings"})
    assert sorted(result.errors) == [
        "field_map['building_count'] = 'numbuildings' is not a column of the dataset",
        "field_map['work_class'] = 'workclass' is not a column of the dataset",
    ]


def test_fetch_columns_reads_field_names():
    def handler(request):
        assert request.url.path == "/api/views/abcd-1234/columns.json"
        return httpx.Response(200, json=[{"fieldName": "permitnum"}, {"fieldName": "statuscurrent"}])

    cols = asyncio.run(fetch_columns("https://data.city.gov", "abcd-1234", httpx.MockTransport(handler)))
    assert cols == ["permitnum", "statuscurrent"]


def test_fetch_columns_404_is_source_unavailable():
    with pytest.raises(SourceUnavailable):
        asyncio.run(fetch_columns("https://data.city.gov", "nope",
                                  httpx.MockTransport(lambda r: httpx.Response(404))))

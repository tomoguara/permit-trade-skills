"""API smoke test (Stage 04 deliverable 5) against a live test database.

Seed: `pts seeds seattle`, then `SyncEngine.sync(local_file=...)` over the
first 400 rows of the Seattle export (data/wa-seattle-building-permits.json),
which exercises the real normalization + embedding path. The live Seattle
Socrata API is unavailable, so the export stands in for it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from conftest import DATA_TABLES, JURISDICTION, ROOT, copy_rows, insert_jurisdiction, make_permit_rows
from permit_trade_skills.server import create_app
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.query.nlu import NLUParser
from permit_trade_skills.query.service import DbNluCache, QueryService
from permit_trade_skills.query.tools import TOOL_SCHEMAS, call_tool
from permit_trade_skills.settings import DEFAULTS, _merge
from query_fakes import nlu_payload

EXPORT = ROOT / "data" / "wa-seattle-building-permits.json"
SEED_ROWS = 400
SETTINGS = _merge(DEFAULTS, {"api": {"rate_limit_per_minute": 0}})


def head_of_json_array(path: Path, n: int) -> list:
    """First n objects of a (large) top-level JSON array, without parsing the whole file."""
    decoder = json.JSONDecoder()
    rows, buf = [], ""
    with open(path) as f:
        buf = f.read(1 << 20).lstrip()
        assert buf.startswith("["), "expected a JSON array"
        pos = 1
        while len(rows) < n:
            while pos < len(buf) and buf[pos] in " \r\n\t,":
                pos += 1
            try:
                obj, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError:
                more = f.read(1 << 20)
                if not more:
                    break
                buf, pos = buf[pos:] + more, 0
                continue
            rows.append(obj)
            pos = end
    return rows


@pytest.fixture(scope="module")
def seeded(engine, db_url, tmp_path_factory):
    if not EXPORT.exists():
        pytest.skip(f"{EXPORT} not present")
    from permit_trade_skills import app as cli_app
    from permit_trade_skills.ingest.sync import SyncEngine

    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    result = CliRunner().invoke(cli_app, ["seeds", "seattle", "--db-url", db_url])
    assert result.exit_code == 0, result.output
    sample = tmp_path_factory.mktemp("seed") / "seattle-head.json"
    sample.write_text(json.dumps(head_of_json_array(EXPORT, SEED_ROWS)))
    report = asyncio.run(SyncEngine(db_url=db_url).sync(JURISDICTION, local_file=str(sample)))
    assert report.inserted >= 90
    return report


@pytest.fixture(scope="module")
def provider():
    return FakeProvider(nlu_payload(status=["ISSUED"], permit_class="Residential",
                                    ambiguous=["residential: interpreted as permit_class=Residential"],
                                    jurisdiction="Seattle"))


@pytest.fixture(scope="module")
def service(seeded, db_url, provider):
    svc = QueryService(db_url)
    svc._nlu = NLUParser(provider, DbNluCache(svc._sessions))
    return svc


@pytest.fixture(scope="module")
def client(service):
    return TestClient(create_app(service=service, settings=SETTINGS))


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_jurisdictions(client, engine, seeded):
    body = client.get("/api/v1/jurisdictions").json()
    (j,) = body["jurisdictions"]
    assert j["slug"] == JURISDICTION and j["platform"] == "socrata" and j["dataset_id"] == "76t5-zqzr"
    assert j["record_count"] == scalar(engine, "SELECT count(*) FROM permits") == seeded.inserted
    assert j["last_synced_at"].endswith("Z")


def test_permits_filters_and_frozen_shape(client):
    resp = client.get("/api/v1/permits", params={"jurisdiction": JURISDICTION, "status": "ISSUED,COMPLETED",
                                                 "permit_class": "residential", "min_cost": 1000,
                                                 "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"jurisdiction", "query", "total_estimate", "count", "next_cursor", "rows", "trade_coverage", "trace_id"}
    assert body["query"]["status"] == ["ISSUED", "COMPLETED"] and body["query"]["min_cost"] == 1000.0
    assert 0 < body["count"] == len(body["rows"]) <= 10
    for row in body["rows"]:
        assert row["status"] in ("ISSUED", "COMPLETED")
        assert row["permit_class_mapped"] == "Residential" or row["permit_class"].lower() == "residential"
        assert row["valuation"] >= 1000
    assert resp.headers["X-Trace-Id"] == body["trace_id"]


@pytest.mark.parametrize("params,status,code", [
    ({"jurisdiction": "nowhere-xx"}, 404, "JURISDICTION_NOT_FOUND"),
    ({"jurisdiction": JURISDICTION, "issued_after": "2026-09-01", "issued_before": "2026-01-01"},
     422, "INVALID_DATE_RANGE"),
    ({"jurisdiction": JURISDICTION, "status": "CLOSED"}, 422, "INVALID_PARAMETER"),
    ({"jurisdiction": JURISDICTION, "cursor": "garbage"}, 422, "INVALID_CURSOR"),
    ({}, 422, "INVALID_PARAMETER"),
])
def test_permits_errors_are_problem_json(client, params, status, code):
    resp = client.get("/api/v1/permits", params=params)
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["error_code"] == code and resp.json()["trace_id"]


def test_keyset_paging_three_pages_equal_one_page_of_75(client):
    base = {"jurisdiction": JURISDICTION, "sort": "issued_at_desc"}
    numbers, cursor = [], None
    for _ in range(3):
        body = client.get("/api/v1/permits", params={**base, "page_size": 25,
                                                      **({"cursor": cursor} if cursor else {})}).json()
        numbers += [r["permit_number"] for r in body["rows"]]
        cursor = body["next_cursor"]
    single = client.get("/api/v1/permits", params={**base, "page_size": 75}).json()
    assert len(numbers) == 75 == len(set(numbers))
    assert numbers == [r["permit_number"] for r in single["rows"]]


@pytest.mark.parametrize("params,where", [
    ({}, "TRUE"),
    ({"since": "2023-01-01", "until": "2026-01-01"},
     "applied_at >= '2023-01-01' AND applied_at < '2026-01-01'"),
    ({"since": "2024-01-01", "date_field": "issued_at", "permit_class": "Residential"},
     "issued_at >= '2024-01-01' AND (lower(permit_class) = 'residential' "
     "OR lower(permit_class_mapped) = 'residential')"),
])
def test_status_counts_equal_independent_group_by(client, engine, params, where):
    body = client.get("/api/v1/status-counts", params={"jurisdiction": JURISDICTION, **params}).json()
    with engine.connect() as conn:
        expected = dict(conn.execute(text(
            f"SELECT status, count(*) FROM permits WHERE jurisdiction_slug = :j AND {where} GROUP BY status"),
            {"j": JURISDICTION}).all())
    for status in ("PENDING", "ISSUED", "IN_INSPECTION", "COMPLETED", "EXPIRED", "CANCELED"):
        expected.setdefault(status, 0)   # zero buckets are listed
    if not expected.get("UNKNOWN"):
        expected.pop("UNKNOWN", None)    # UNKNOWN only when > 0
    assert body["counts"] == expected
    assert body["total"] == sum(expected.values())


def test_get_permit_with_snapshot_and_inference(client, engine, db_url):
    number = scalar(engine, "SELECT permit_number FROM permits ORDER BY id LIMIT 1")
    url = f"/api/v1/permits/{number}"
    body = client.get(url, params={"jurisdiction": JURISDICTION}).json()
    assert body["permit"]["permit_number"] == number
    assert body["detail_snapshot"] == {"present": False, "fetched_at": None, "final_url": None,
                                       "fetch_strategy": None}
    assert body["skill_inference"] is None and body["status_history"] == []

    from permit_trade_skills.store import transaction
    from permit_trade_skills.store.repos import InferenceRepository, PermitRepository, SnapshotRepository

    with transaction(db_url) as s:
        pid = PermitRepository(s).get(JURISDICTION, number).id
        snap = SnapshotRepository(s).upsert_snapshot(pid, "https://example.test/p", "soda_row", "scope text")
        InferenceRepository(s).upsert_inference(pid, "skill-infer-v1", "fake/m",
                                                [{"skill": "Framing", "evidence": "scope text"}],
                                                input_snapshot_id=snap)
    body = client.get(url, params={"jurisdiction": JURISDICTION}).json()
    assert body["detail_snapshot"]["present"] is True
    assert body["skill_inference"]["skills"][0]["skill"] == "Framing"

    missing = client.get("/api/v1/permits/NOPE-1", params={"jurisdiction": JURISDICTION})
    assert missing.status_code == 404 and missing.json()["error_code"] == "PERMIT_NOT_FOUND"


def test_nl_query_round_trip_and_db_cache(client, provider, engine):
    calls_before = len(provider.calls)
    question = "issued residential permits in Seattle"
    first = client.post("/api/v1/query", json={"question": question, "page_size": 5}).json()
    assert first["query"]["status"] == ["ISSUED"] and first["executed"] is True
    assert first["echo"].startswith("Showing the first 5 ISSUED permits, Residential class")
    structured = client.get("/api/v1/permits", params={"jurisdiction": JURISDICTION, "status": "ISSUED",
                                                       "permit_class": "Residential", "page_size": 5}).json()
    assert [r["permit_number"] for r in first["rows"]] == [r["permit_number"] for r in structured["rows"]]

    second = client.post("/api/v1/query", json={"question": question, "page_size": 5}).json()
    assert second["nlu"]["cached"] is True and first["nlu"]["cached"] is False
    assert len(provider.calls) == calls_before + 1
    assert scalar(engine, "SELECT count(*) FROM nlu_cache") >= 1
    assert second["rows"] == first["rows"]


@pytest.mark.parametrize("name,args", [
    ("list_jurisdictions", {}),
    ("search_permits", {"status": ["ISSUED"], "page_size": 3}),
    ("search_permits", {"text": "addition", "k": 5}),
    ("permit_status_counts", {"since": "2020-01-01"}),
])
def test_tools_live(service, name, args):
    result = call_tool(name, args, service)
    assert result["ok"] is True, result
    jsonschema.validate(result, TOOL_SCHEMAS[name]["returns"])
    if "text" in args:
        rows = result["data"]["rows"]
        assert rows and all("addition" in (r["description"] or "").lower() or "vector" in r["matched_by"]
                            for r in rows)


def test_get_permit_tool_and_errors(service, engine):
    number = scalar(engine, "SELECT permit_number FROM permits ORDER BY id LIMIT 1")
    ok = call_tool("get_permit", {"permit_number": number}, service)
    assert ok["ok"] and ok["data"]["permit"]["permit_number"] == number
    missing = call_tool("get_permit", {"permit_number": "NOPE-1"}, service)
    assert missing == {"ok": False, "error_code": "PERMIT_NOT_FOUND", "retryable": False,
                       "message": missing["message"]}


def test_retrieve_degrades_without_embeddings(service, engine, db_url):
    # A second jurisdiction loaded from the synthetic fixture: descriptions, no embeddings.
    other = "fixture-noembed-wa"
    with engine.begin() as conn:
        insert_jurisdiction(conn, other)
    copy_rows(db_url, make_permit_rows(300, jurisdiction=other))
    degraded = service.retrieve(other, {}, "deck addition", k=10, modes=("text", "vector"))
    text_only = service.retrieve(other, {}, "deck addition", k=10, modes=("text",))
    assert degraded.degraded is True and degraded.modes_used == ["text"]
    assert degraded.fused_order == text_only.fused_order and degraded.fused_order
    # With two jurisdictions registered, tools need an explicit jurisdiction.
    result = call_tool("search_permits", {"status": ["ISSUED"]}, service)
    assert result["error_code"] == "JURISDICTION_REQUIRED"

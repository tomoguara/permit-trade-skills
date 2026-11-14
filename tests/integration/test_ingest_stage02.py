"""Stage 02 against the test DB with a fake Socrata server (no live calls).

P1 rate limit + User-Agent, P2 window/--since/fingerprint, P3 schema drift,
exact dry runs, embedding reuse, no-API jurisdictions, the --full guard,
sync-report details, and acceptance criteria AC1, AC5, AC6.
"""

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from permit_trade_skills import app
from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.discovery.schemas import Jurisdiction, PlatformType
from permit_trade_skills.ingest import sync as sync_module
from permit_trade_skills.ingest.soda_client import SodaClient
from permit_trade_skills.ingest.sync import SyncEngine

JID = "rivertown-clark-wa"
FIELD_MAP = {"permit_num": "permitnum", "status_current": "statuscurrent", "issue_date": "issueddate",
             "applied_at": "applieddate", "description": "description", "work_class": "workclass"}
TODAY = date.today()


@pytest.fixture
def embed_calls(monkeypatch):
    calls = []

    def fake(texts):
        calls.append(list(texts))
        return [[0.1] * 384 for _ in texts]

    monkeypatch.setattr(sync_module, "compute_embeddings", fake)
    return calls


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(SodaClient, "BACKOFF_BASE", 0.0)


def register(db_url, **strategy):
    registry = Registry(db_url)
    session = registry.get_session()
    try:
        registry.add_jurisdiction(session, Jurisdiction(
            id=JID, name="Rivertown", state="WA", platform_type=PlatformType.SOCRATA,
            base_url="https://data.rivertown.gov", dataset_id="rt01-perm",
            field_map=FIELD_MAP,
            status_map={"issued": ["Issued"], "completed": ["Completed"], "pending": ["In Review"]},
            detail_strategy={"timestamp_field": "issueddate",
                             "detail_url_rule": "https://p.rivertown.gov/{{permitnum}}", **strategy},
        ), replaced_by="test")
    finally:
        session.close()


@pytest.fixture
def jurisdiction(clean_db, db_url):
    register(db_url)
    return JID


def rows(n, issued_offset_days=400, applied_offset_days=400, status="Issued", desc="work"):
    issued = (TODAY - timedelta(days=issued_offset_days)).isoformat()
    applied = (TODAY - timedelta(days=applied_offset_days)).isoformat()
    return [{":id": f"row-{i}", ":updated_at": "2026-09-26T01:11:06.798Z", "permitnum": f"R-{i:04d}",
             "statuscurrent": status, "issueddate": f"{issued}T00:00:00.000",
             "applieddate": f"{applied}T00:00:00.000", "description": f"{desc} {i}",
             "workclass": "Residential"} for i in range(n)]


def run(db_url, fake=None, **kw):
    transport = httpx.MockTransport(fake) if fake else None
    return asyncio.run(SyncEngine(db_url, soda_transport=transport).sync(kw.pop("slug", JID), **kw))


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


# ---- P3 schema drift --------------------------------------------------------

def test_missing_core_column_fails_fast_and_writes_nothing(jurisdiction, db_url, clean_db, make_soda_page,
                                                           embed_calls):
    data = rows(5)
    fake = make_soda_page(data, columns=["permitnum", "statuscurrent", "applieddate", "description"])
    report = run(db_url, fake, full=True)
    assert report.error and "issue_date -> issueddate" in report.error
    warning = report.warnings[-1]
    assert warning["type"] == "schema_violation" and warning["retryable"] is False
    assert warning["missing"] == {"issue_date": "issueddate", "timestamp_field": "issueddate"}
    assert fake.row_requests == []  # failed before pulling any row
    assert scalar(clean_db, "SELECT count(*) FROM permits WHERE jurisdiction_id = :j", j=JID) == 0
    assert scalar(clean_db, "SELECT error FROM sync_metadata WHERE jurisdiction_id = :j", j=JID) == report.error


def test_missing_other_column_warns_and_syncs(jurisdiction, db_url, clean_db, make_soda_page, embed_calls):
    data = rows(5)
    for r in data:
        del r["workclass"]
    report = run(db_url, make_soda_page(data), full=True)
    assert report.error is None and report.inserted == 5
    warning = next(w for w in report.warnings if w["type"] == "missing_column")
    assert warning["columns"] == {"work_class": "workclass"}


def test_columns_fetch_failure_is_a_failed_run(jurisdiction, db_url, make_soda_page, embed_calls):
    report = run(db_url, make_soda_page(rows(2), columns_status=503), full=True)
    assert report.error and report.warnings[-1]["type"] == "source_unavailable"


# ---- P2 incremental window, --since, fingerprint ------------------------------

def test_incremental_where_rules():
    w = SyncEngine.incremental_where
    today = date(2026, 11, 9)
    assert w(None, "issueddate", "applieddate", 90, None, today) is None
    assert w("2026-10-01", "issueddate", "applieddate", 90, None, today) == \
        "(applieddate >= '2026-08-11') OR (issueddate > '2026-10-01')"
    assert w("2026-10-01", "issueddate", None, 90, None, today) == "issueddate > '2026-10-01'"
    assert w("2026-10-01", "issueddate", "applieddate", 120, None, today).startswith("(applieddate >= '2026-07-12')")
    assert w(None, "issueddate", "applieddate", 90, date(2026, 1, 1), today) == "applieddate >= '2026-01-01'"
    assert w(None, "issueddate", None, 90, date(2026, 1, 1), today) == "issueddate >= '2026-01-01'"


def test_incremental_re_pulls_recently_applied_permits(jurisdiction, db_url, clean_db, make_soda_page,
                                                       embed_calls):
    old = rows(3, issued_offset_days=300, applied_offset_days=320)            # outside the window
    recent = rows(2, issued_offset_days=200, applied_offset_days=30, desc="recent")  # inside it
    for i, r in enumerate(recent):
        r["permitnum"] = f"N-{i}"
    assert run(db_url, make_soda_page(old + recent), full=True).inserted == 5
    # A recently applied permit's status changes, but its issue date is before the cursor.
    recent[0]["statuscurrent"] = "Completed"
    fake = make_soda_page(old + recent)
    report = run(db_url, fake)
    assert report.mode == "incremental"
    assert "(applieddate >= " in fake.row_requests[0].url.params["$where"]
    assert (report.pull_count, report.updated, report.unchanged) == (2, 1, 1)
    assert report.status_transitions == {"ISSUED->COMPLETED": 1}
    assert report.max_applied_at.date() == TODAY - timedelta(days=30)


def test_since_replaces_window_start(jurisdiction, db_url, make_soda_page, embed_calls):
    fake = make_soda_page(rows(3, applied_offset_days=10))
    report = run(db_url, fake, since=TODAY - timedelta(days=15))
    assert report.mode == "incremental" and report.pull_count == 3
    assert fake.row_requests[0].url.params["$where"] == \
        f"applieddate >= '{(TODAY - timedelta(days=15)).isoformat()}'"
    with pytest.raises(ValueError, match="--since"):
        run(db_url, fake, since=TODAY, full=True)


def test_identical_pull_is_a_no_op_and_full_reprocesses(jurisdiction, db_url, clean_db, tmp_path,
                                                        embed_calls):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows(4)))
    first = run(db_url, local_file=str(path))
    assert first.inserted == 4 and first.fingerprint
    synced = scalar(clean_db, "SELECT max(synced_at) FROM permits WHERE jurisdiction_id = :j", j=JID)

    again = run(db_url, local_file=str(path))
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 4)
    assert again.warnings[-1]["type"] == "identical_pull" and again.fingerprint == first.fingerprint
    assert scalar(clean_db, "SELECT max(synced_at) FROM permits WHERE jurisdiction_id = :j", j=JID) == synced

    forced = run(db_url, local_file=str(path), full=True)
    assert (forced.inserted, forced.updated, forced.unchanged) == (0, 0, 4)
    assert all(w["type"] != "identical_pull" for w in forced.warnings)

    register(db_url)  # config_version 2 -> a different fingerprint
    assert all(w["type"] != "identical_pull" for w in run(db_url, local_file=str(path)).warnings)


# ---- dry run, embeddings, provenance, state ---------------------------------

def test_dry_run_counts_exactly_and_writes_nothing(jurisdiction, db_url, clean_db, tmp_path, embed_calls):
    data = rows(4)
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    data[0]["statuscurrent"] = "Completed"
    data.append({**data[1], "permitnum": "R-NEW", ":id": "row-new"})
    path.write_text(json.dumps(data))
    reports_before = scalar(clean_db, "SELECT count(*) FROM sync_metadata")

    report = run(db_url, local_file=str(path), dry_run=True)
    assert (report.inserted, report.updated, report.unchanged) == (1, 1, 3)
    assert report.status_transitions == {"ISSUED->COMPLETED": 1}
    assert scalar(clean_db, "SELECT count(*) FROM permits WHERE jurisdiction_id = :j", j=JID) == 4
    assert scalar(clean_db, "SELECT count(*) FROM permits WHERE status = 'COMPLETED'") == 0
    assert scalar(clean_db, "SELECT count(*) FROM status_history") == 0
    assert scalar(clean_db, "SELECT count(*) FROM sync_metadata") == reports_before


def test_embeddings_reused_for_unchanged_descriptions(jurisdiction, db_url, tmp_path, embed_calls):
    data = rows(5)
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    assert sum(len(c) for c in embed_calls) == 5
    embed_calls.clear()
    data[2]["description"] = "a new scope of work"
    data[3]["statuscurrent"] = "Completed"  # changed row, same description
    path.write_text(json.dumps(data))
    report = run(db_url, local_file=str(path))
    assert report.updated == 2
    assert embed_calls == [["a new scope of work"]]


def test_whitespace_change_reembeds_once(jurisdiction, db_url, tmp_path, embed_calls):
    data = rows(2)
    data[0]["description"] = "Replace   roof"
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    assert ["Replace roof", "work 1"] in embed_calls


def test_provenance_ac5(jurisdiction, db_url, clean_db, tmp_path, embed_calls):
    data = rows(2)
    del data[1][":updated_at"]
    data[1]["issueddate"] = None
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    report = run(db_url, local_file=str(path))
    with clean_db.connect() as conn:
        got = conn.execute(text(
            "SELECT p.permit_number, p.raw_row IS NOT NULL AS has_raw, p.raw_hash, p.source_updated_at, "
            "p.warnings, m.run_id IS NOT NULL AS linked FROM permits p "
            "LEFT JOIN sync_metadata m ON m.run_id = p.run_id WHERE p.jurisdiction_id = :j "
            "ORDER BY p.permit_number"), {"j": JID}).all()
    assert all(r.has_raw and r.raw_hash and r.linked for r in got)
    assert got[0].source_updated_at is not None
    assert got[1].source_updated_at is None and {"type": "missing_source_updated_at"} in got[1].warnings
    assert report.max_synced_at is not None


def test_resync_keeps_processing_state_ac6(jurisdiction, db_url, clean_db, tmp_path, embed_calls):
    data = rows(2)
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE permits SET processing_state = 'SKILLS_INFERRED' WHERE permit_number = 'R-0000'"))
    data[0]["statuscurrent"] = "Completed"
    path.write_text(json.dumps(data))
    assert run(db_url, local_file=str(path)).updated == 1
    assert scalar(clean_db, "SELECT processing_state FROM permits WHERE permit_number = 'R-0000'") == \
        "SKILLS_INFERRED"


def test_second_identical_full_sync_writes_nothing_ac1(jurisdiction, db_url, clean_db, make_soda_page,
                                                        embed_calls):
    data = rows(30)
    run(db_url, make_soda_page(data), full=True)
    before = scalar(clean_db, "SELECT array_agg(synced_at ORDER BY id) FROM permits")
    second = run(db_url, make_soda_page(data), full=True)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 30)
    assert scalar(clean_db, "SELECT array_agg(synced_at ORDER BY id) FROM permits") == before


# ---- no-API jurisdictions, P1 politeness --------------------------------------

def test_machine_readable_false_refuses_socrata_but_allows_local_file(clean_db, db_url, tmp_path,
                                                                     make_soda_page, embed_calls):
    register(db_url, machine_readable=False)
    fake = make_soda_page(rows(2))
    report = run(db_url, fake)
    assert report.error and "machine_readable=false" in report.error
    assert report.warnings[-1]["retryable"] is False and fake.requests == []
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows(2)))
    assert run(db_url, local_file=str(path)).inserted == 2


def test_user_agent_from_settings(jurisdiction, db_url, make_soda_page, monkeypatch, embed_calls):
    monkeypatch.setenv("PTS_INGEST_USER_AGENT", "PermitTradeSkills/1.0 (+https://example.org/contact)")
    fake = make_soda_page(rows(1))
    run(db_url, fake, full=True)
    assert {r.headers["User-Agent"] for r in fake.requests} == \
        {"PermitTradeSkills/1.0 (+https://example.org/contact)"}


def test_rate_limiter_spaces_requests(monkeypatch):
    from permit_trade_skills.detail.ratelimit import HostRateLimiter

    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    clock = iter(range(0, 1000)).__next__  # each call advances 1 s
    limiter = HostRateLimiter(30, clock=lambda: float(clock()), sleep=fake_sleep)  # 2 s apart

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        async with SodaClient("https://data.x.gov/resource/a", transport=transport,
                              rate_limiter=limiter, user_agent="ua") as client:
            for _ in range(3):
                await client.list_rows({})

    asyncio.run(go())
    assert len(waits) == 2 and all(w > 0 for w in waits)


def test_engine_rate_limiter_uses_registry_override(monkeypatch):
    monkeypatch.setenv("PTS_INGEST_RATE_LIMIT_PER_MINUTE", "60")
    from permit_trade_skills.discovery.schemas import JurisdictionConfig

    j = JurisdictionConfig(id="x", name="x", state="WA", platform_type="socrata", config_version=1,
                           detail_strategy={"detail": {"rate_limits_per_minute": {"cos-data.seattle.gov": 20}}})
    limiter = SyncEngine.rate_limiter(j)
    assert limiter.rate("cos-data.seattle.gov") == 20 and limiter.rate("other.gov") == 60


# ---- CLI: --full guard, sync-report ------------------------------------------

def test_full_needs_yes_for_socrata(jurisdiction, db_url, monkeypatch):
    async def fake_count(self, slug):
        return 193_249

    monkeypatch.setattr(SyncEngine, "count_source_rows", fake_count)
    result = CliRunner().invoke(app, ["sync", "sync", "-j", JID, "--full", "--db-url", db_url])
    assert result.exit_code == 1
    assert "193,249 rows in 194 requests" in result.output and "--yes" in result.output


def test_full_local_file_needs_no_yes(jurisdiction, db_url, tmp_path, embed_calls):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows(1)))
    result = CliRunner().invoke(app, ["sync", "sync", "-j", JID, "--full", "--local-file", str(path),
                                      "--db-url", db_url])
    assert result.exit_code == 0, result.output


def test_sync_report_shows_details(jurisdiction, db_url, tmp_path, embed_calls):
    data = rows(2)
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    data[0]["statuscurrent"] = "Mystery"
    path.write_text(json.dumps(data))
    run(db_url, local_file=str(path))
    out = CliRunner().invoke(app, ["sync", "sync-report", "-j", JID, "--last", "1", "--db-url", db_url]).output
    assert "Mode: full, config version 1" in out
    assert "Status transitions: ISSUED->UNKNOWN 1" in out
    assert "Warning: unknown_status" in out
    js = tmp_path / "r.json"
    CliRunner().invoke(app, ["sync", "sync-report", "-j", JID, "-o", str(js), "--db-url", db_url])
    first = json.loads(js.read_text())[0]
    assert first["fingerprint"] and first["status_transitions"] == {"ISSUED->UNKNOWN": 1}

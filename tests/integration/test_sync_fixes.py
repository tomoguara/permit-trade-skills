"""SyncEngine against a fake Socrata transport and the test DB.

Regression tests for the Stage 01-02 sync bugs: B3 (config export
wired), B4 (row endpoint), B5 (errors abort and are reported), B6 (no row cap,
stable order), B7 (dropped rows counted), B8 (prefix slugs), B9 (no Seattle
defaults), B10 (no silent overwrite). No live Socrata calls.
"""

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
import yaml
from sqlalchemy import text
from typer.testing import CliRunner

from permit_trade_skills.discovery import DiscoveryEngine, JurisdictionExistsError
from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.discovery.schemas import DiscoveryDossier, Jurisdiction, PlatformType
from permit_trade_skills.ingest import sync as sync_module
from permit_trade_skills.ingest.soda_client import SodaClient
from permit_trade_skills.ingest.sync import SyncEngine
from permit_trade_skills.store.doctor import run_doctor

JID = "testville-lane-or"
BASE_URL = "https://data.testville.gov"
DATASET = "tv01-perm"
RULE = "https://portal.testville.gov/record?altId={{permitnum}}"


@pytest.fixture(autouse=True)
def no_embeddings_no_backoff(monkeypatch):
    monkeypatch.setattr(sync_module, "compute_embeddings", lambda texts: [])
    monkeypatch.setattr(SodaClient, "BACKOFF_BASE", 0.0)


@pytest.fixture
def jurisdiction(clean_db, db_url):
    registry = Registry(db_url)
    session = registry.get_session()
    try:
        registry.add_jurisdiction(session, Jurisdiction(
            id=JID, name="Testville", state="OR", county="Lane", platform_type=PlatformType.SOCRATA,
            base_url=BASE_URL, api_endpoint=f"{BASE_URL}/api/views", dataset_id=DATASET,
            field_map={"permit_num": "permitnum", "status_current": "statuscurrent",
                       "issue_date": "issueddate", "city": "originalcity", "state": "originalstate",
                       "description": "description"},
            status_map={"issued": ["Issued"], "completed": ["Completed"]},
            detail_strategy={"timestamp_field": "issueddate", "detail_url_rule": RULE},
        ))
    finally:
        session.close()
    return JID


def make_rows(n, start=date(2026, 1, 1)):
    return [{":id": f"row-{i}", "permitnum": f"P-{i:05d}", "statuscurrent": "Issued",
             "issueddate": (start + timedelta(days=i % 300)).isoformat() + "T00:00:00.000",
             "description": f"work {i}"} for i in range(n)]


from soda_fakes import FakeSocrata  # noqa: E402  (also the make_soda_page fixture)


COLUMNS = ["permitnum", "statuscurrent", "issueddate", "originalcity", "originalstate", "description"]


def run_sync(db_url, fake=None, **kw):
    transport = httpx.MockTransport(fake) if fake else None
    return asyncio.run(SyncEngine(db_url, soda_transport=transport).sync(kw.pop("slug", JID), **kw))


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_full_pull_uses_resource_endpoint_and_stable_order(jurisdiction, db_url, clean_db):
    fake = FakeSocrata(make_rows(2500))
    report = run_sync(db_url, fake, full=True)
    assert report.error is None and report.mode == "full"
    assert (report.pull_count, report.inserted, report.dropped) == (2500, 2500, 0)
    assert len(fake.row_requests) == 3  # 1000 + 1000 + 500
    assert {r.url.path for r in fake.row_requests} == {f"/resource/{DATASET}.json"}  # B4
    assert {r.url.params["$order"] for r in fake.row_requests} == {"permitnum ASC"}  # P4
    assert scalar(clean_db, "SELECT count(DISTINCT permit_number) FROM permits WHERE jurisdiction_id = :j",
                  j=JID) == 2500
    assert report.cursor_after == "2026-10-27"  # day 299 after 2026-01-01


def test_incremental_run_uses_cursor_and_is_idempotent(jurisdiction, db_url, clean_db):
    rows = make_rows(50)
    run_sync(db_url, FakeSocrata(rows), full=True)
    fake = FakeSocrata(rows + [{"permitnum": "NEW-1", "statuscurrent": "Issued",
                                "issueddate": "2026-12-01T00:00:00.000"}])
    report = run_sync(db_url, fake)
    assert report.mode == "incremental" and report.cursor_before == "2026-02-19"
    assert fake.row_requests[0].url.params["$where"] == "issueddate > '2026-02-19'"
    assert (report.pull_count, report.inserted, report.updated) == (1, 1, 0)
    assert report.cursor_after == "2026-12-01"


def test_error_mid_pull_writes_nothing_and_saves_failed_run(jurisdiction, db_url, clean_db):
    run_sync(db_url, FakeSocrata(make_rows(10)), full=True)
    before = scalar(clean_db, "SELECT count(*) FROM permits")
    report = run_sync(db_url, FakeSocrata(make_rows(2500), fail_on_page=2), full=True)
    assert report.error and report.pull_count == 0
    assert report.warnings[-1]["type"] == "source_unavailable" and report.warnings[-1]["retryable"] is True
    assert scalar(clean_db, "SELECT count(*) FROM permits") == before
    with clean_db.connect() as conn:
        saved = conn.execute(text(
            "SELECT error, cursor_before, cursor_after FROM sync_metadata "
            "WHERE jurisdiction_id = :j ORDER BY id DESC LIMIT 1"), {"j": JID}).one()
    assert saved.error == report.error
    assert saved.cursor_after == saved.cursor_before  # cursor never moves on failure


def test_failed_run_keeps_cursor_for_next_incremental(jurisdiction, db_url, clean_db):
    run_sync(db_url, FakeSocrata(make_rows(10)), full=True)            # cursor 2026-01-10
    run_sync(db_url, FakeSocrata([], columns=COLUMNS, fail_on_page=1, status=503))      # fails
    fake = FakeSocrata(make_rows(10))
    report = run_sync(db_url, fake)
    assert report.cursor_before == "2026-01-10" and report.mode == "incremental"


def test_4xx_is_reported_not_retryable(jurisdiction, db_url, clean_db):
    fake = FakeSocrata([], columns=COLUMNS, fail_on_page=1, status=404)
    report = run_sync(db_url, fake, full=True)
    assert report.error and report.warnings[-1]["retryable"] is False
    assert len(fake.row_requests) == 1


def test_rows_without_permit_number_are_counted_as_dropped(jurisdiction, db_url, clean_db):
    rows = make_rows(5)
    rows[1]["permitnum"] = ""
    del rows[3]["permitnum"]
    for dry_run in (True, False):
        report = run_sync(db_url, FakeSocrata(rows), full=True, dry_run=dry_run)
        assert (report.pull_count, report.dropped) == (5, 2)
    assert report.inserted + report.updated + report.unchanged + report.dropped == report.pull_count
    warning = next(w for w in report.warnings if w["type"] == "missing_permit_number")
    assert warning["count"] == 2
    # `row` is the position in the pulled (permitnum-ordered) list; blanks sort first
    assert warning["rows"] == [{"row": 0, ":id": "row-1"}, {"row": 1, ":id": "row-3"}]
    assert scalar(clean_db, "SELECT dropped FROM sync_metadata WHERE jurisdiction_id = :j "
                            "ORDER BY id DESC LIMIT 1", j=JID) == 2


def test_prefix_resolves_to_full_id(jurisdiction, db_url, clean_db, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(make_rows(3)))
    report = run_sync(db_url, slug="testville", local_file=str(path))
    assert report.jurisdiction_slug == JID
    assert scalar(clean_db, "SELECT count(*) FROM permits WHERE jurisdiction_slug = :j", j=JID) == 3
    assert scalar(clean_db, "SELECT count(*) FROM sync_metadata WHERE jurisdiction_slug = :j", j=JID) == 1


def test_ambiguous_or_unknown_prefix_is_an_error(jurisdiction, db_url, clean_db, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text("[]")
    with pytest.raises(LookupError, match="not found"):
        run_sync(db_url, slug="nowhere", local_file=str(path))
    # clean_db registers seattle-kingcounty-wa; add a second "seattle..." jurisdiction
    from conftest import insert_jurisdiction
    with clean_db.begin() as conn:
        insert_jurisdiction(conn, "seattle-other-wa")
    with pytest.raises(LookupError, match="ambiguous"):
        run_sync(db_url, slug="seattle", local_file=str(path))


def test_detail_url_rule_and_state_default(jurisdiction, db_url, clean_db, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(make_rows(1)))
    run_sync(db_url, local_file=str(path))
    with clean_db.connect() as conn:
        row = conn.execute(text("SELECT detail_url, city, state FROM permits WHERE jurisdiction_id = :j"),
                           {"j": JID}).one()
    assert row.detail_url == "https://portal.testville.gov/record?altId=P-00000"
    assert (row.city, row.state) == (None, "OR")


def test_doctor_flags_slug_mismatch(jurisdiction, db_url, clean_db, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(make_rows(2)))
    run_sync(db_url, local_file=str(path))
    assert run_doctor(clean_db).ok
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE permits SET jurisdiction_slug = 'testville' WHERE jurisdiction_id = :j"),
                     {"j": JID})
    check = next(c for c in run_doctor(clean_db).checks if c.name == "jurisdiction_slug_mismatch")
    assert check.count == 2 and check.severity == "hard"


def test_register_refuses_to_overwrite_without_replace(jurisdiction, db_url, clean_db):
    dossier = DiscoveryDossier(jurisdiction_id=JID, jurisdiction_name="Testville",
                               candidates=[], verification_results=[])
    engine = DiscoveryEngine(db_url=db_url)
    new = DiscoveryEngine.jurisdiction_from_dossier(dossier, "Testville", "OR")
    with pytest.raises(JurisdictionExistsError):
        asyncio.run(engine.register(dossier, new))
    assert scalar(clean_db, "SELECT config_version FROM jurisdictions WHERE id = :j", j=JID) == 1
    assert scalar(clean_db, "SELECT field_map->>'permit_num' FROM jurisdictions WHERE id = :j",
                  j=JID) == "permitnum"
    asyncio.run(engine.register(dossier, new, replace=True))
    assert scalar(clean_db, "SELECT config_version FROM jurisdictions WHERE id = :j", j=JID) == 2


def test_register_cli_exits_1_on_existing_id(jurisdiction, db_url, clean_db, tmp_path):
    from permit_trade_skills import app

    dossier = tmp_path / "dossier.json"
    dossier.write_text(json.dumps({"jurisdiction_id": JID, "jurisdiction_name": "Testville",
                                   "candidates": [], "verification_results": [],
                                   "field_mapping": {"permit_num": "permitnum"}}))
    for argv in (["discovery", "register"], ["register", "jurisdiction"]):
        result = CliRunner().invoke(app, argv + ["-d", str(dossier), "-n", "T", "-s", "OR",
                                                 "--db-url", db_url])
        assert result.exit_code == 1 and "--replace" in result.output
    assert scalar(clean_db, "SELECT config_version FROM jurisdictions WHERE id = :j", j=JID) == 1


def test_config_export_command(jurisdiction, db_url, tmp_path):
    from permit_trade_skills import app

    out = tmp_path / "testville.yaml"
    result = CliRunner().invoke(app, ["config", "export", "-j", JID, "-o", str(out), "--db-url", db_url])
    assert result.exit_code == 0, result.output
    exported = yaml.safe_load(out.read_text())
    assert exported["field_map"]["permit_num"] == "permitnum" and exported["config_version"] == 1
    missing = CliRunner().invoke(app, ["config", "export", "-j", "nope", "-o", str(out), "--db-url", db_url])
    assert missing.exit_code == 1


def test_local_file_discovery_registers_canonical_status_map(clean_db, db_url, tmp_path):
    rows = [{"permitnum": "1", "statuscurrent": "Issued"},
            {"permitnum": "2", "statuscurrent": "Reviews Completed", "issueddate": "2026-01-01"},
            {"permitnum": "3", "statuscurrent": "Who Knows"}]
    out = tmp_path / "dossier.json"
    dossier = asyncio.run(DiscoveryEngine(db_url=db_url).discover_from_local_file(
        rows, "Testville", "OR", output_path=out))
    assert dossier.status_mapping == {"issued": ["Issued"], "pending": ["Reviews Completed"]}
    assert "Who Knows" in dossier.notes
    assert dossier.field_mapping["issue_date"] == "issueddate"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dossier.json"]  # no _data.json copy

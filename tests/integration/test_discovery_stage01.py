"""Stage 01 against the test DB with fake HTTP (no live catalog or Socrata calls).

Discovery runs + evidence (D4), score breakdown and --approve (D3), register
--dry-run (R5) and validation (R4), config history (R1), export defaults (R2),
typed Registry.get, and a second mock jurisdiction end to end (Definition of done).
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import text
from typer.testing import CliRunner

from permit_trade_skills import app
from permit_trade_skills.discovery import DiscoveryEngine
from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.ingest import sync as sync_module
from permit_trade_skills.ingest.sync import SyncEngine

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "discovery" / "mock_bellevue_permits.json"
PACKAGES = [
    {"id": "pkg-permits", "title": "Testville Building Permits", "url": "https://data.testville.gov/d/tv01-perm",
     "notes": "Permits", "organization": {"title": "City of Testville"},
     "tags": [{"name": "permits"}, {"name": "building"}], "resources": [{"format": "CSV", "url": "x"}]},
    {"id": "pkg-parks", "title": "Parks", "url": "https://parks.example.org/parks",
     "organization": {"title": "Parks Dept"}},
]


def fake_web(request):
    """catalog.data.gov search + platform probes."""
    host, path = request.url.host, request.url.path
    if host == "catalog.data.gov" and path.endswith("/package_search"):
        return httpx.Response(200, json={"result": {"count": 2, "results": PACKAGES}})
    if host == "data.testville.gov" and path == "/api/views.json":
        return httpx.Response(200, json={"meta": {}})
    return httpx.Response(404)


def cli(*args):
    result = CliRunner().invoke(app, list(args))
    return result


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


@pytest.fixture
def no_embeddings(monkeypatch):
    monkeypatch.setattr(sync_module, "compute_embeddings", lambda texts: [])


def discover(db_url, evidence):
    engine = DiscoveryEngine(db_url=db_url, transport=httpx.MockTransport(fake_web))
    return asyncio.run(engine.discover("Testville", "OR", evidence_dir=evidence))


def test_discover_records_run_breakdown_and_evidence(clean_db, db_url, tmp_path):
    dossier = discover(db_url, tmp_path / "evidence")
    top = dossier.candidates[0]
    assert top.id == "pkg-permits" and top.platform_type.value == "socrata"
    assert set(top.score_breakdown) == {"title_match", "organization_match", "keyword_match",
                                        "platform_confidence", "metadata_quality"}
    assert abs(sum(f["weighted"] for f in top.score_breakdown.values()) - top.confidence) < 1e-3

    with clean_db.connect() as conn:
        run = conn.execute(text("SELECT jurisdiction_id, status, candidates_found, search_parameters "
                                "FROM discovery_runs WHERE id = :id"), {"id": dossier.discovery_run_id}).one()
    assert run.jurisdiction_id is None and run.status == "completed" and run.candidates_found == 2
    assert run.search_parameters["jurisdiction_id"] == "testville-or"

    files = list((tmp_path / "evidence" / "testville-or" / "discovery").glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith(f"-{dossier.discovery_run_id[:8]}.json")
    evidence = json.loads(files[0].read_text())
    assert "metadata" not in evidence["candidates"][0]
    assert evidence["discovery_run"]["id"] == dossier.discovery_run_id


def test_register_approve_dry_run_force_and_link(clean_db, db_url, tmp_path):
    dossier = discover(db_url, None)
    path = tmp_path / "dossier.json"
    data = dossier.model_dump(mode="json")
    path.write_text(json.dumps(data))
    base = ["discovery", "register", "-d", str(path), "-n", "Testville", "-s", "OR", "--db-url", db_url]

    # A catalog dossier has no maps: validation blocks it, dry-run says why.
    blocked = cli(*base)
    assert blocked.exit_code == 1 and "permit_num" in blocked.output and "--force" in blocked.output
    plan = cli(*base, "--dry-run", "--approve", "pkg-parks")
    assert plan.exit_code == 1
    plan_json = json.loads(plan.stdout[plan.stdout.index("{"):])
    assert plan_json["action"].startswith("refused: validation errors")
    assert plan_json["candidate"]["id"] == "pkg-parks"
    assert scalar(clean_db, "SELECT count(*) FROM jurisdictions WHERE id = 'testville-or'") == 0

    unknown = cli(*base, "--approve", "nope")
    assert unknown.exit_code == 1 and "not in the dossier" in unknown.output

    # Fill in the maps; dry-run now plans an insert and still writes nothing.
    data["field_mapping"] = {"permit_num": "permitnum", "status_current": "statuscurrent"}
    data["status_mapping"] = {"issued": ["Issued"]}
    path.write_text(json.dumps(data))
    plan = cli(*base, "--dry-run")
    assert plan.exit_code == 0 and '"action": "insert (config_version 1)"' in plan.stdout
    assert scalar(clean_db, "SELECT count(*) FROM jurisdictions WHERE id = 'testville-or'") == 0

    done = cli(*base, "--approve", "pkg-parks")
    assert done.exit_code == 0, done.output
    with clean_db.connect() as conn:
        row = conn.execute(text("SELECT dataset_id, platform_type FROM jurisdictions "
                                "WHERE id = 'testville-or'")).one()
        linked = conn.execute(text("SELECT jurisdiction_id FROM discovery_runs WHERE id = :id"),
                              {"id": dossier.discovery_run_id}).scalar()
    assert row.dataset_id == "pkg-parks" and row.platform_type == "other"
    assert linked == "testville-or"

    replace_plan = cli(*base, "--dry-run", "--replace")
    assert "replace (config_version 1 -> 2)" in replace_plan.stdout


def test_config_history_records_replaced_versions(clean_db, db_url, tmp_path):
    assert cli("seeds", "seattle", "--db-url", db_url).exit_code == 0       # clean_db row v1 -> v2
    assert cli("seeds", "seattle", "--db-url", db_url).exit_code == 0       # v2 -> v3
    listing = cli("config", "history", "-j", "seattle-kingcounty-wa", "--db-url", db_url)
    assert listing.exit_code == 0
    lines = listing.output.strip().splitlines()[1:]
    assert [line.split()[0] for line in lines] == ["1", "2", "3"]
    assert "pts seeds seattle" in lines[1] and "(current)" in lines[2]

    v1 = yaml.safe_load(cli("config", "history", "-j", "seattle-kingcounty-wa", "-v", "1",
                            "--db-url", db_url).output)
    assert v1["config_version"] == 1 and v1["field_map"] == {}   # the clean_db placeholder row
    v2 = yaml.safe_load(cli("config", "history", "-j", "seattle-kingcounty-wa", "-v", "2",
                            "--db-url", db_url).output)
    assert v2["field_map"]["permit_num"] == "permitnum" and "api_key" not in v2
    assert cli("config", "history", "-j", "seattle-kingcounty-wa", "-v", "9",
               "--db-url", db_url).exit_code == 1


def test_export_default_path_and_seed_exports_db_row(clean_db, db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "seed.yaml"
    assert cli("seeds", "seattle", "-o", str(out), "--db-url", db_url).exit_code == 0
    assert yaml.safe_load(out.read_text())["config_version"] == 2  # the DB row, not the seed's 1
    result = cli("config", "export", "-j", "seattle-kingcounty-wa", "--db-url", db_url)
    assert result.exit_code == 0
    exported = yaml.safe_load((tmp_path / "config/jurisdictions/seattle-kingcounty-wa.yaml").read_text())
    assert exported["detail_strategy"]["detail_url_rule"].endswith("altId={{permitnum}}")


def test_seed_check_columns_reports_unverified_columns(clean_db, db_url, monkeypatch):
    from permit_trade_skills.cli.seeds import SEATTLE_FIELD_MAP
    from permit_trade_skills.ingest import soda_client

    async def fake_columns(base_url, dataset_id, transport=None):
        assert (base_url, dataset_id) == ("https://cos-data.seattle.gov", "76t5-zqzr")
        return [c for c in SEATTLE_FIELD_MAP.values() if c not in ("workclass", "numbuildings")]

    monkeypatch.setattr(soda_client, "fetch_columns", fake_columns)
    result = cli("seeds", "seattle", "--check-columns", "--db-url", db_url)
    assert result.exit_code == 1
    assert "'workclass' is not a column" in result.output and "nothing registered" in result.output
    assert scalar(clean_db, "SELECT config_version FROM jurisdictions WHERE id = 'seattle-kingcounty-wa'") == 1


def test_registry_get_is_typed(clean_db, db_url):
    assert cli("seeds", "seattle", "--db-url", db_url).exit_code == 0
    config = Registry(db_url).get("seattle")
    assert config.id == config.slug == "seattle-kingcounty-wa"
    assert config.field_map["permit_num"] == "permitnum"
    assert "Issued" in config.status_map["issued"]
    with pytest.raises(LookupError):
        Registry(db_url).get("nowhere")


def test_second_mock_jurisdiction_end_to_end(clean_db, db_url, tmp_path, no_embeddings):
    """Runbook demo (docs/discovery.md): discover from an export, register, sync. No network."""
    dossier_path = tmp_path / "bellevue.json"
    result = cli("discovery", "discover", "-j", "Bellevue", "-s", "WA", "--local-file", str(FIXTURE),
                 "-o", str(dossier_path), "--evidence-dir", str(tmp_path / "evidence"), "--db-url", db_url)
    assert result.exit_code == 0, result.output
    assert "Mystery Hold" in result.output  # unmatched status reported in the notes

    dossier = json.loads(dossier_path.read_text())
    assert dossier["field_mapping"]["permit_num"] == "permit_number"
    assert dossier["field_mapping"]["detail_url"] == "detail_link"
    assert dossier["status_mapping"]["completed"] == ["Finaled"]

    result = cli("discovery", "register", "-d", str(dossier_path), "-n", "Bellevue", "-s", "WA",
                 "-c", "King County", "--db-url", db_url)
    assert result.exit_code == 0, result.output
    assert "timestamp_field is not set" in result.output  # warning only

    report = asyncio.run(SyncEngine(db_url).sync("bellevue", local_file=str(FIXTURE)))
    assert (report.jurisdiction_id, report.pull_count, report.inserted, report.dropped) == \
        ("bellevue-wa", 10, 10, 0)
    with clean_db.connect() as conn:
        statuses = dict(conn.execute(text(
            "SELECT status, count(*) FROM permits WHERE jurisdiction_id = 'bellevue-wa' GROUP BY 1")).all())
        url = conn.execute(text("SELECT detail_url FROM permits WHERE permit_number = 'BLV-24000'")).scalar()
    assert statuses == {"ISSUED": 3, "COMPLETED": 1, "PENDING": 2, "CANCELED": 1, "EXPIRED": 1,
                        "IN_INSPECTION": 1, "UNKNOWN": 1}
    assert url == "https://permits.example-bellevue.test/record/BLV-24000"

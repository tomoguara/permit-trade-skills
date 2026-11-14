"""Migration chain: fresh upgrade, one-step round trip, full base round trip,
single head, model/DB parity, destructive-revision refusal, baseline stamping."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from conftest import TEST_DB_URL, recreate_database
from permit_trade_skills.discovery.registry import Base
from permit_trade_skills.store import schema

SCRATCH_URL = make_url(TEST_DB_URL).set(
    database=make_url(TEST_DB_URL).database.replace("_test", "_migrations_test")
).render_as_string(hide_password=False)


@pytest.fixture
def scratch(db_url):  # db_url: skips when Postgres is unavailable
    recreate_database(SCRATCH_URL)
    eng = create_engine(SCRATCH_URL)
    yield eng
    eng.dispose()


def _schema_snapshot(engine):
    insp = inspect(engine)
    return {t: sorted(c["name"] for c in insp.get_columns(t))
            for t in insp.get_table_names() if t != "alembic_version"}


def test_single_head():
    assert schema.heads() == ["0009_evaluation"]


def test_fresh_upgrade_then_round_trip(scratch):
    assert schema.upgrade(scratch) == ["0001_initial", "0002_canonical_store", "0003_nlu_cache",
                                       "0004_detail_extraction", "0005_skill_inference_audit",
                                       "0006_jurisdiction_history", "0007_sync_fingerprint",
                                       "0008_orchestration", "0009_evaluation"]
    at_head = _schema_snapshot(scratch)
    assert {"labels", "build_gates", "drift_alerts"} <= set(at_head)
    assert {"suite", "cache_key", "payload"} <= set(at_head["eval_results"])
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0008_orchestration"
    assert "labels" not in _schema_snapshot(scratch) and "suite" not in _schema_snapshot(scratch)["eval_results"]
    assert "orchestration_events" in at_head and "proposed_by" in at_head["skill_inferences"]
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0007_sync_fingerprint"
    assert "orchestration_events" not in _schema_snapshot(scratch)
    assert "proposed_by" not in _schema_snapshot(scratch)["skill_inferences"]
    assert "fingerprint" in at_head["sync_metadata"]
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0006_jurisdiction_history"
    assert "fingerprint" not in _schema_snapshot(scratch)["sync_metadata"]
    assert "jurisdiction_versions" in _schema_snapshot(scratch)
    assert "input_cache_key" in at_head["skill_inferences"]
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0005_skill_inference_audit"
    assert "jurisdiction_versions" not in _schema_snapshot(scratch)
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0004_detail_extraction"
    assert "input_cache_key" not in _schema_snapshot(scratch)["skill_inferences"]
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0003_nlu_cache"
    assert "detail_fetch_runs" not in _schema_snapshot(scratch)
    assert "fields" not in _schema_snapshot(scratch)["detail_snapshots"]
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0002_canonical_store"
    assert "nlu_cache" not in _schema_snapshot(scratch)
    schema.downgrade(scratch, "-1")
    assert schema.status(scratch).current == "0001_initial"
    assert "detail_snapshots" not in _schema_snapshot(scratch)
    schema.upgrade(scratch)
    assert _schema_snapshot(scratch) == at_head
    # full history: base -> head
    schema.downgrade(scratch, "base")
    assert _schema_snapshot(scratch) == {}
    schema.upgrade(scratch)
    assert _schema_snapshot(scratch) == at_head


def test_models_match_migrated_schema(engine):
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}),
                                 Base.metadata)
    assert diffs == []


def test_destructive_revision_refused_without_flag(scratch, monkeypatch):
    schema.upgrade(scratch, "0001_initial")
    monkeypatch.setattr(schema, "is_destructive", lambda s: s.revision == "0002_canonical_store")
    with pytest.raises(schema.DestructiveMigrationError):
        schema.ensure_schema(scratch)
    assert schema.status(scratch).current == "0001_initial"
    assert schema.upgrade(scratch, allow_destructive=True) == ["0002_canonical_store", "0003_nlu_cache",
                                                                  "0004_detail_extraction", "0005_skill_inference_audit",
                                                                  "0006_jurisdiction_history",
                                                                  "0007_sync_fingerprint", "0008_orchestration",
                                                                  "0009_evaluation"]


def test_pre_alembic_database_is_stamped_at_baseline(scratch):
    # Simulate a database built by create_all before Alembic: baseline tables, no alembic_version.
    schema.upgrade(scratch, "0001_initial")
    with scratch.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    st = schema.status(scratch)
    later = ["0002_canonical_store", "0003_nlu_cache", "0004_detail_extraction", "0005_skill_inference_audit",
             "0006_jurisdiction_history", "0007_sync_fingerprint", "0008_orchestration", "0009_evaluation"]
    assert st.pre_alembic and st.pending == later
    assert schema.ensure_schema(scratch) == later
    assert schema.status(scratch).current == "0009_evaluation"

"""Shared fixtures for DB-backed tests.

Tests run against a dedicated, disposable database (default
`permit_trade_skills_test` on the local Docker Postgres; override with
PTS_TEST_DB_URL). It is dropped and recreated once per session and migrated
from scratch with Alembic, so every run also proves `upgrade head` on a fresh
database. The live database is never touched: URLs whose database name does
not end in `_test` are refused.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))  # gen_fixture

TEST_DB_URL = os.environ.get(
    "PTS_TEST_DB_URL", "postgresql://postgres:postgres@localhost:5432/permit_trade_skills_test"
)
JURISDICTION = "seattle-kingcounty-wa"
DATA_TABLES = ("labels", "build_gates", "drift_alerts", "orchestration_events", "pipeline_runs", "retry_backlog", "detail_fetch_results", "detail_fetch_runs", "nlu_cache", "eval_results", "skill_inferences", "detail_snapshots", "status_history",
               "sync_metadata", "permits", "probes", "status_maps", "discovery_runs", "jurisdiction_versions",
               "jurisdictions")


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(hide_password=False)


def recreate_database(url: str) -> None:
    name = make_url(url).database
    if not name or not name.endswith("_test"):
        raise RuntimeError(f"Refusing to recreate non-test database {name!r}")
    admin = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def db_url() -> str:
    try:
        recreate_database(TEST_DB_URL)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"test Postgres unavailable ({e.__class__.__name__}: {e})")
    from permit_trade_skills.store import schema

    applied = schema.upgrade(TEST_DB_URL)
    assert applied, "fresh database should have migrations to apply"
    return TEST_DB_URL


@pytest.fixture(scope="session")
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


def insert_jurisdiction(conn, jurisdiction_id: str = JURISDICTION) -> None:
    conn.execute(text(
        "INSERT INTO jurisdictions (id, name, state, platform_type, base_url, config_version, "
        "field_map, status_map, detail_strategy, is_active, created_at, updated_at) "
        "VALUES (:id, 'Test', 'WA', 'socrata', 'https://example.test', 1, '{}', '{}', '{}', true, "
        "now(), now()) ON CONFLICT (id) DO NOTHING"
    ), {"id": jurisdiction_id})


@pytest.fixture
def clean_db(engine):
    """Empty all tables and register the test jurisdiction."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
        insert_jurisdiction(conn)
    return engine


@pytest.fixture
def session(clean_db):
    from sqlalchemy.orm import Session

    with Session(clean_db) as s:
        yield s
        s.rollback()


def make_permit_rows(n: int, seed: int = 7, jurisdiction: str = JURISDICTION,
                     anchor: datetime | None = None):
    from gen_fixture import generate

    rows = list(generate(n, seed=seed, jurisdiction=jurisdiction, anchor_date=anchor))
    for r in rows:
        r["jurisdiction_id"] = jurisdiction
        r["jurisdiction_slug"] = jurisdiction
    return rows


def copy_rows(db_url: str, rows) -> int:
    import psycopg
    from gen_fixture import load_rows

    with psycopg.connect(db_url) as conn:
        n = load_rows(conn, rows)
        conn.execute("ANALYZE permits")
    return n


@pytest.fixture(autouse=True)
def no_ingest_rate_limit(monkeypatch):
    """Socrata calls go to fake transports in tests: don't space them out."""
    monkeypatch.setenv("PTS_INGEST_RATE_LIMIT_PER_MINUTE", "0")


@pytest.fixture
def make_soda_page():
    """Factory for a fake Socrata server (tests/soda_fakes.py):
    `fake = make_soda_page(rows, columns=None, fail_on_page=None, status=500)`;
    pass `httpx.MockTransport(fake)` to SodaClient / SyncEngine(soda_transport=...)."""
    from soda_fakes import FakeSocrata

    return FakeSocrata

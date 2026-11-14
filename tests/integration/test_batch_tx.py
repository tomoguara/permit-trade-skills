"""Sync upserts a whole run in one transaction: all-or-nothing."""

import pytest
from sqlalchemy import text

from conftest import JURISDICTION
from permit_trade_skills.ingest.sync import SyncEngine


def _normalized(n, run_id="run-1", status="ISSUED"):
    from permit_trade_skills.ingest.normalizers import compute_raw_hash
    from datetime import datetime

    now = datetime(2026, 11, 1)
    rows = []
    for i in range(n):
        raw = {"permitnum": f"B-{i}", "statuscurrent": status}
        rows.append({
            "jurisdiction_id": JURISDICTION, "jurisdiction_slug": JURISDICTION,
            "permit_number": f"B-{i}", "status": status, "status_raw": status.title(),
            "raw_row": raw, "raw_hash": compute_raw_hash(raw), "run_id": run_id,
            "created_at": now, "updated_at": now, "synced_at": now,
            "processing_state": "SYNCED", "warnings": [],
        })
    return rows


def _count(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM permits")).scalar()


def test_10k_batch_commits_in_one_transaction(clean_db, db_url):
    inserted, updated, unchanged, _ = SyncEngine(db_url)._upsert_rows(_normalized(10_000))
    assert (inserted, updated, unchanged) == (10_000, 0, 0)
    assert _count(clean_db) == 10_000


def test_error_at_row_5000_rolls_back_everything(clean_db, db_url, monkeypatch):
    # Seed 100 existing rows so the failed run also contains updates.
    SyncEngine(db_url)._upsert_rows(_normalized(100))
    before = _count(clean_db)

    engine = SyncEngine(db_url)
    original = engine._insert_permit
    calls = {"n": 0}

    def flaky_insert(session, permit):
        calls["n"] += 1
        if calls["n"] == 4_900:  # row 5000 overall (100 updates come first)
            raise RuntimeError("injected failure at row 5000")
        return original(session, permit)

    monkeypatch.setattr(engine, "_insert_permit", flaky_insert)
    rows = _normalized(10_000, run_id="run-2", status="COMPLETED")
    with pytest.raises(RuntimeError, match="row 5000"):
        engine._upsert_rows(rows)

    assert _count(clean_db) == before
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM permits WHERE status = 'COMPLETED'")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM status_history")).scalar() == 0


def test_status_transitions_written_to_history(clean_db, db_url):
    SyncEngine(db_url)._upsert_rows(_normalized(10))
    rows = _normalized(10, run_id="run-2")
    for r in rows[:3]:
        r["status"], r["status_raw"] = "COMPLETED", "Completed"
    inserted, updated, unchanged, transitions = SyncEngine(db_url)._upsert_rows(rows)
    assert (inserted, updated, unchanged) == (0, 3, 7)
    assert transitions == {"ISSUED->COMPLETED": 3}
    with clean_db.connect() as conn:
        hist = conn.execute(text(
            "SELECT from_status, to_status, raw_status, run_id FROM status_history")).all()
        run_ids = {r[0] for r in conn.execute(text(
            "SELECT run_id FROM permits WHERE status = 'COMPLETED'"))}
    assert hist == [("ISSUED", "COMPLETED", "Completed", "run-2")] * 3
    assert run_ids == {"run-2"}  # run_id = last run that changed the row

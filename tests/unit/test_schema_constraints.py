"""Schema integrity: FK cascades, uniqueness, CHECK constraints, optimistic state CAS."""

import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from conftest import JURISDICTION, copy_rows, make_permit_rows
from permit_trade_skills.store.repos import PermitRepository


def _one_permit(db_url, **overrides):
    row = make_permit_rows(1)[0]
    row.update(overrides)
    copy_rows(db_url, [row])
    return row


def _expect_violation(engine, sql, params=None):
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})


def test_unique_permit_number_per_jurisdiction(clean_db, db_url):
    _one_permit(db_url)
    with pytest.raises(Exception, match="idx_permit_jurisdiction_number"):
        _one_permit(db_url)


def test_status_check_rejects_unknown_value(clean_db, db_url):
    _one_permit(db_url)
    _expect_violation(clean_db, "UPDATE permits SET status = 'APPROVED'")


def test_processing_state_check(clean_db, db_url):
    _one_permit(db_url)
    _expect_violation(clean_db, "UPDATE permits SET processing_state = 'BOGUS'")


@pytest.mark.parametrize("col,value", [("latitude", 91), ("latitude", -90.5),
                                       ("longitude", 180.1), ("longitude", -181)])
def test_geo_checks(clean_db, db_url, col, value):
    _one_permit(db_url)
    _expect_violation(clean_db, f"UPDATE permits SET {col} = :v", {"v": value})


def test_geo_point_is_generated(clean_db, db_url):
    _one_permit(db_url, latitude=47.6, longitude=-122.3)
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT geo[0], geo[1] FROM permits")).one() == (-122.3, 47.6)


def test_cascade_delete_from_jurisdiction(clean_db, db_url):
    _one_permit(db_url)
    with clean_db.begin() as conn:
        pid = conn.execute(text("SELECT id FROM permits")).scalar()
        conn.execute(text("INSERT INTO status_history (permit_id, jurisdiction_id, to_status) "
                          "VALUES (:p, :j, 'ISSUED')"), {"p": pid, "j": JURISDICTION})
        conn.execute(text("INSERT INTO detail_snapshots (permit_id, final_url, fetch_strategy, "
                          "content_text, content_hash) VALUES (:p, 'u', 'soda_row', 't', 'h')"), {"p": pid})
        conn.execute(text("INSERT INTO skill_inferences (permit_id, prompt_version, model, skills) "
                          "VALUES (:p, 'v1', 'm', '[]')"), {"p": pid})
        conn.execute(text("DELETE FROM jurisdictions WHERE id = :j"), {"j": JURISDICTION})
        counts = conn.execute(text(
            "SELECT (SELECT count(*) FROM permits), (SELECT count(*) FROM status_history), "
            "(SELECT count(*) FROM detail_snapshots), (SELECT count(*) FROM skill_inferences)"
        )).one()
    assert tuple(counts) == (0, 0, 0, 0)


def test_config_version_check(clean_db):
    _expect_violation(clean_db, "UPDATE jurisdictions SET config_version = 0")


def test_advance_state_is_compare_and_set(session, db_url):
    _one_permit(db_url)
    pid = session.execute(text("SELECT id FROM permits")).scalar()
    repo = PermitRepository(session)
    assert repo.advance_state(pid, "SYNCED", "DETAIL_READY") is True
    assert repo.advance_state(pid, "SYNCED", "DETAIL_READY") is False  # already advanced
    assert repo.advance_state(pid, "DETAIL_READY", "DETAIL_FAILED",
                              error={"stage": "detail", "error": "timeout", "retryable": True}) is True
    session.commit()
    state, err = session.execute(text("SELECT processing_state, state_error FROM permits")).one()
    assert state == "DETAIL_FAILED" and err["retryable"] is True
    with pytest.raises(ValueError):
        repo.advance_state(pid, "DETAIL_FAILED", "NOT_A_STATE")


def test_concurrent_advance_state_exactly_one_winner(clean_db, db_url):
    """2 threads x 100 iterations racing SYNCED -> DETAIL_READY on one permit."""
    _one_permit(db_url)
    with clean_db.connect() as conn:
        pid = conn.execute(text("SELECT id FROM permits")).scalar()

    for _ in range(100):
        with clean_db.begin() as conn:
            conn.execute(text("UPDATE permits SET processing_state = 'SYNCED' WHERE id = :p"), {"p": pid})
        barrier = threading.Barrier(2)
        results = []

        def worker():
            with Session(clean_db) as s:
                barrier.wait()
                ok = PermitRepository(s).advance_state(pid, "SYNCED", "DETAIL_READY")
                s.commit()
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True], results
        with clean_db.connect() as conn:
            assert conn.execute(text("SELECT processing_state FROM permits WHERE id = :p"),
                                {"p": pid}).scalar() == "DETAIL_READY"


def test_touch_batch(session, db_url):
    copy_rows(db_url, make_permit_rows(5))
    ids = [r[0] for r in session.execute(text("SELECT id FROM permits ORDER BY id"))]
    assert PermitRepository(session).touch(ids[:3], "SKILLS_SKIPPED") == 3
    session.commit()
    assert session.execute(text(
        "SELECT count(*) FROM permits WHERE processing_state = 'SKILLS_SKIPPED'")).scalar() == 3

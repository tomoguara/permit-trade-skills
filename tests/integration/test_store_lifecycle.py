"""Snapshot/inference repositories, retention pruning, and the permits fillfactor."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from conftest import JURISDICTION, copy_rows, make_permit_rows
from permit_trade_skills.store.repos import InferenceRepository, SnapshotRepository
from permit_trade_skills.store.retention import prune


def _permit_id(session):
    return session.execute(text("SELECT id FROM permits ORDER BY id LIMIT 1")).scalar()


def test_snapshot_upsert_is_idempotent_per_content(session, db_url):
    copy_rows(db_url, make_permit_rows(1))
    pid = _permit_id(session)
    repo = SnapshotRepository(session)
    a = repo.upsert_snapshot(pid, "https://x/1", "soda_row", "content v1", raw_html="<p>1</p>")
    b = repo.upsert_snapshot(pid, "https://x/1b", "soda_row", "content v1", http_status=200)
    c = repo.upsert_snapshot(pid, "https://x/2", "playwright", "content v2")
    session.commit()
    assert a == b != c
    assert session.execute(text("SELECT count(*) FROM detail_snapshots")).scalar() == 2
    first = session.execute(text("SELECT final_url, raw_html, http_status FROM detail_snapshots "
                                 "WHERE id = :i"), {"i": a}).one()
    assert tuple(first) == ("https://x/1b", "<p>1</p>", 200)  # raw_html kept when not re-sent
    assert repo.latest_for(pid).id == c


def test_inference_repo(session, db_url):
    copy_rows(db_url, make_permit_rows(2))
    pid = _permit_id(session)
    repo = InferenceRepository(session)
    first = repo.upsert_inference(pid, "v1", "model-a", [{"skill": "roofing"}], request_id="r1")
    again = repo.upsert_inference(pid, "v1", "model-a", [{"skill": "roofing"}], request_id="r1")
    second = repo.upsert_inference(pid, "v2", "model-a", [{"skill": "framing"}])
    session.commit()
    assert first == again != second
    assert repo.latest_for(pid).id == second
    assert [i.id for i in repo.pending_inferences(10, jurisdiction=JURISDICTION)] == [first, second]
    assert repo.pending_inferences(10, jurisdiction="elsewhere") == []
    session.execute(text("UPDATE skill_inferences SET review_status = 'APPROVED' WHERE id = :i"),
                    {"i": first})
    assert [i.id for i in repo.pending_inferences(10)] == [second]


def test_prune_raw_html_and_sync_runs(session, db_url):
    copy_rows(db_url, make_permit_rows(1))
    pid = _permit_id(session)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=91)
    session.execute(text(
        "INSERT INTO detail_snapshots (permit_id, final_url, fetch_strategy, content_text, raw_html, "
        "content_hash, fetched_at) VALUES "
        "(:p, 'u', 'soda_row', 'old', '<old>', 'h1', :old), (:p, 'u', 'soda_row', 'new', '<new>', 'h2', now())"
    ), {"p": pid, "old": old})
    session.execute(text(
        "INSERT INTO sync_metadata (jurisdiction_id, jurisdiction_slug, run_id, config_version, pull_count, "
        "inserted, updated, unchanged, dropped, duration_ms, started_at, completed_at) "
        "SELECT :j, :j, 'run-' || g, 1, 0, 0, 0, 0, 0, 0, now() - g * interval '1 hour', now() "
        "FROM generate_series(1, 515) g"
    ), {"j": JURISDICTION})

    dry = prune(session, dry_run=True)
    assert (dry.raw_html_nulled, dry.sync_runs_deleted) == (1, 3)
    result = prune(session)
    session.commit()
    assert (result.raw_html_nulled, result.sync_runs_deleted) == (1, 3)
    html = dict(session.execute(text("SELECT content_text, raw_html FROM detail_snapshots")).all())
    assert html == {"old": None, "new": "<new>"}  # content_text never pruned
    kept = [r[0] for r in session.execute(text("SELECT run_id FROM sync_metadata"))]
    assert len(kept) == 512 and "run-513" not in kept and "run-1" in kept


def test_permits_fillfactor_and_status_churn(clean_db, db_url):
    """FILLFACTOR=90 is set; 10k status updates + VACUUM leave bounded growth."""
    copy_rows(db_url, make_permit_rows(10_000))
    with clean_db.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        opts = conn.execute(text("SELECT reloptions FROM pg_class WHERE relname = 'permits'")).scalar()
        assert "fillfactor=90" in opts
        conn.execute(text("VACUUM FULL permits"))  # rewrite pages with the fillfactor applied
        before = conn.execute(text("SELECT pg_relation_size('permits')")).scalar()
        conn.execute(text("UPDATE permits SET status = CASE status WHEN 'ISSUED' THEN 'COMPLETED' "
                          "ELSE 'ISSUED' END"))
        conn.execute(text("VACUUM permits"))
        conn.execute(text("UPDATE permits SET status = CASE status WHEN 'ISSUED' THEN 'COMPLETED' "
                          "ELSE 'ISSUED' END"))
        conn.execute(text("VACUUM permits"))
        after = conn.execute(text("SELECT pg_relation_size('permits')")).scalar()
    # Each full-table churn needs at most one extra copy of the live rows; vacuum makes that
    # space reusable, so two churn rounds must not grow the heap beyond ~2x.
    assert after <= before * 2.1, (before, after)

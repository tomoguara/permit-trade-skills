"""`pts db doctor` finds injected corruption and exits non-zero."""

from sqlalchemy import text
from typer.testing import CliRunner

from conftest import JURISDICTION, copy_rows, make_permit_rows
from permit_trade_skills import app
from permit_trade_skills.store.doctor import run_doctor


def test_clean_fixture_passes(clean_db, db_url):
    copy_rows(db_url, make_permit_rows(200))
    report = run_doctor(clean_db)
    assert report.ok, [c for c in report.hard_failures]


def test_detects_orphan_history_and_3_hash_mismatches(clean_db, db_url):
    copy_rows(db_url, make_permit_rows(200))
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE permits SET raw_hash = 'tampered' "
                          "WHERE id IN (SELECT id FROM permits ORDER BY id LIMIT 3)"))
        # Bypass FK triggers to plant an orphan (as a botched manual delete/restore would).
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("INSERT INTO status_history (permit_id, jurisdiction_id, to_status) "
                          "VALUES (999999, :j, 'ISSUED')"), {"j": JURISDICTION})

    report = run_doctor(clean_db, sample_size=1000)
    by_name = {c.name: c for c in report.checks}
    assert by_name["orphan_status_history"].count == 1
    assert by_name["raw_hash_mismatch"].count == 3
    assert sum(c.count for c in report.hard_failures) == 4
    assert not report.ok

    result = CliRunner().invoke(app, ["db", "doctor", "--db-url", db_url])
    assert result.exit_code == 1
    assert "orphan_status_history: 1" in result.output
    assert "raw_hash_mismatch: 3" in result.output


def test_issued_without_date_is_report_only(clean_db, db_url):
    rows = make_permit_rows(5)
    rows[0].update(status="ISSUED", issued_at=None, issue_date=None)
    copy_rows(db_url, rows)
    report = run_doctor(clean_db)
    assert {c.name: c.count for c in report.checks}["issued_without_issued_at"] == 1
    assert report.ok

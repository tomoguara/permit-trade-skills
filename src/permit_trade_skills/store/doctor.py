"""`pts db doctor`: post-migration integrity checks on the canonical store.

Hard failures (non-zero exit): orphan status_history rows, raw_hash
mismatches, duplicate permit numbers within a jurisdiction, tables without an
audit timestamp column. Report-only warnings: ISSUED permits with no
issued_at, database not at the migration head.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from permit_trade_skills.ingest.normalizers import compute_raw_hash

HARD = "hard"
WARN = "warn"
AUDIT_COLUMNS = {"created_at", "executed_at", "changed_at", "started_at", "fetched_at"}
IGNORED_TABLES = {"alembic_version"}


@dataclass
class CheckResult:
    name: str
    severity: str
    count: int
    description: str
    examples: List[Any] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.count > 0


@dataclass
class DoctorReport:
    checks: List[CheckResult]

    @property
    def hard_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if c.failed and c.severity == HARD]

    @property
    def ok(self) -> bool:
        return not self.hard_failures


def _orphan_status_history(conn) -> CheckResult:
    rows = conn.execute(text(
        "SELECT h.id, h.permit_id FROM status_history h "
        "LEFT JOIN permits p ON p.id = h.permit_id WHERE p.id IS NULL"
    )).all()
    return CheckResult("orphan_status_history", HARD, len(rows),
                       "status_history rows whose permit no longer exists",
                       [f"status_history.id={r.id} permit_id={r.permit_id}" for r in rows[:10]])


def _raw_hash_mismatches(conn, sample_size: int) -> CheckResult:
    rows = conn.execute(text(
        "SELECT id, permit_number, raw_row, raw_hash FROM permits "
        "ORDER BY random() LIMIT :n"
    ), {"n": sample_size}).all()
    bad = [r for r in rows if compute_raw_hash(r.raw_row) != r.raw_hash]
    return CheckResult("raw_hash_mismatch", HARD, len(bad),
                       f"raw_hash differs from the recomputed hash of raw_row "
                       f"(sampled {len(rows)} permits; data-corruption alarm)",
                       [f"permits.id={r.id} {r.permit_number}" for r in bad[:10]])


def _duplicate_permit_numbers(conn) -> CheckResult:
    rows = conn.execute(text(
        "SELECT jurisdiction_id, permit_number, count(*) AS n FROM permits "
        "GROUP BY 1, 2 HAVING count(*) > 1"
    )).all()
    return CheckResult("duplicate_permit_number", HARD, len(rows),
                       "permit numbers appearing more than once within a jurisdiction",
                       [f"{r.jurisdiction_id} {r.permit_number} x{r.n}" for r in rows[:10]])


def _slug_mismatches(conn) -> CheckResult:
    """Rows saved under a prefix slug by `pts sync sync -j <prefix>` before the fix."""
    rows = conn.execute(text(
        "SELECT 'permits' AS tbl, jurisdiction_id, jurisdiction_slug, count(*) AS n FROM permits "
        "WHERE jurisdiction_slug <> jurisdiction_id GROUP BY 1, 2, 3 "
        "UNION ALL "
        "SELECT 'sync_metadata', jurisdiction_id, jurisdiction_slug, count(*) FROM sync_metadata "
        "WHERE jurisdiction_slug <> jurisdiction_id GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    )).all()
    return CheckResult("jurisdiction_slug_mismatch", HARD, sum(r.n for r in rows),
                       "rows whose jurisdiction_slug differs from jurisdiction_id "
                       "(slug queries miss them; repair SQL in docs/ops.md)",
                       [f"{r.tbl}: {r.jurisdiction_slug!r} != {r.jurisdiction_id!r} x{r.n}" for r in rows[:10]])


def _missing_audit_columns(engine: Engine) -> CheckResult:
    insp = inspect(engine)
    missing = []
    for table in sorted(set(insp.get_table_names()) - IGNORED_TABLES):
        cols = {c["name"] for c in insp.get_columns(table)}
        if not cols & AUDIT_COLUMNS:
            missing.append(table)
    return CheckResult("missing_audit_column", HARD, len(missing),
                       f"tables without any of {sorted(AUDIT_COLUMNS)}", missing)


def _issued_without_date(conn) -> CheckResult:
    n = conn.execute(text(
        "SELECT count(*) FROM permits WHERE status = 'ISSUED' AND issued_at IS NULL"
    )).scalar()
    examples = [r.permit_number for r in conn.execute(text(
        "SELECT permit_number FROM permits WHERE status = 'ISSUED' AND issued_at IS NULL "
        "ORDER BY id LIMIT 5"
    ))]
    return CheckResult("issued_without_issued_at", WARN, n,
                       "permits with status ISSUED but no issued_at (report-only)", examples)


def _schema_at_head(engine: Engine) -> CheckResult:
    from permit_trade_skills.store.schema import status

    st = status(engine)
    behind = [] if st.current == st.head else [f"current={st.current} head={st.head}"]
    return CheckResult("schema_not_at_head", WARN, len(behind),
                       "database is behind the latest migration (run `pts db migrate`)", behind)


def run_doctor(engine: Engine, sample_size: int = 1000) -> DoctorReport:
    with engine.connect() as conn:
        checks = [
            _orphan_status_history(conn),
            _raw_hash_mismatches(conn, sample_size),
            _duplicate_permit_numbers(conn),
            _slug_mismatches(conn),
            _issued_without_date(conn),
        ]
    checks.append(_missing_audit_columns(engine))
    checks.append(_schema_at_head(engine))
    return DoctorReport(checks)

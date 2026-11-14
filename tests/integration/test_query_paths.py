"""Query paths: hot-path index use at 200k rows, keyset paging at page 500,
trigram keyword search, and status+date filter correctness."""

import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from conftest import JURISDICTION, copy_rows, insert_jurisdiction, make_permit_rows
from permit_trade_skills.store.query import PermitQuery
from permit_trade_skills.store.repos import PermitRepository

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "permits_200k.jsonl.gz"
ANCHOR = datetime(2026, 11, 1)  # gen_fixture's default anchor date
HOT_PATH = (
    "SELECT id, permit_number, issued_at FROM permits "
    "WHERE jurisdiction_slug = :slug AND status = 'ISSUED' "
    "AND issued_at >= CAST(:anchor AS timestamp) - interval '90 days' "
    "ORDER BY issued_at DESC"
)


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


@pytest.mark.slow
def test_hot_path_200k_uses_index_under_5ms(clean_db, db_url):
    from gen_fixture import load_fixture, write_fixture

    if not FIXTURE.exists():
        write_fixture(FIXTURE, 200_000)
    assert load_fixture(db_url, FIXTURE) == 200_000

    timings, buffers = [], []
    with clean_db.connect() as conn:
        for _ in range(20):
            plan = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + HOT_PATH),
                                {"slug": JURISDICTION, "anchor": ANCHOR}).scalar()
            plan = plan if isinstance(plan, list) else json.loads(plan)
            root = plan[0]
            nodes = list(_plan_nodes(root["Plan"]))
            assert not any(n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "permits"
                           for n in nodes), "hot path must not Seq Scan permits"
            assert any(n.get("Index Name") == "idx_permits_slug_status_issued" for n in nodes)
            timings.append(root["Execution Time"])
            buffers.append(root["Plan"].get("Shared Hit Blocks", 0) + root["Plan"].get("Shared Read Blocks", 0))
        rows = conn.execute(text(HOT_PATH), {"slug": JURISDICTION, "anchor": ANCHOR}).all()

    p95 = statistics.quantiles(timings, n=20)[18]
    assert rows, "fixture should contain recently issued permits"
    assert p95 < 5.0, f"hot path p95 {p95:.2f} ms"
    assert max(buffers) < 20_000, f"buffers {max(buffers)}"


@pytest.fixture
def paging_db(clean_db, db_url):
    """~25k permits for paging/filter tests."""
    copy_rows(db_url, make_permit_rows(25_000, seed=11))
    return clean_db


def _all_ids(session, order_sql):
    return [r[0] for r in session.execute(text(
        f"SELECT id FROM permits WHERE jurisdiction_slug = :j ORDER BY {order_sql}"), {"j": JURISDICTION})]


@pytest.mark.parametrize("sort,order_sql", [
    ("issued_at_desc", "issued_at DESC NULLS LAST, id DESC"),
    ("cost_desc", "valuation DESC NULLS LAST, id DESC"),
    ("number_asc", "permit_number ASC, id ASC"),
])
def test_keyset_page_500_matches_full_scan(paging_db, sort, order_sql):
    with Session(paging_db) as s:
        expected = _all_ids(s, order_sql)
        page = PermitRepository(s).list(PermitQuery(jurisdiction=JURISDICTION, sort=sort,
                                                     page_size=50, page=500))
    assert [p.id for p in page.rows] == expected[24_950:25_000]
    assert page.next_cursor is None  # last page


def test_cursor_walk_covers_all_rows_without_duplicates(paging_db):
    """Walk every page by cursor; NULL issued_at rows (PENDING) are paged too."""
    with Session(paging_db) as s:
        repo = PermitRepository(s)
        expected = _all_ids(s, "issued_at DESC NULLS LAST, id DESC")
        seen, cursor = [], None
        while True:
            page = repo.list(PermitQuery(jurisdiction=JURISDICTION, page_size=200, cursor=cursor))
            seen.extend(p.id for p in page.rows)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
    assert seen == expected
    assert len(seen) == len(set(seen)) == 25_000


def test_gin_trigram_serves_keyword_search(paging_db):
    with paging_db.connect() as conn:
        conn.execute(text("SET enable_seqscan = off"))  # small table: prove the index is usable
        plan = "\n".join(r[0] for r in conn.execute(text(
            "EXPLAIN SELECT id FROM permits WHERE description ILIKE '%roof repl%'")))
    assert "gin_permits_description_trgm" in plan
    with Session(paging_db) as s:
        page = PermitRepository(s).list(PermitQuery(jurisdiction=JURISDICTION, desc_keyword="ROOF REPL",
                                                    page_size=200))
        assert page.rows and all("roof repl" in p.description.lower() for p in page.rows)


def test_status_and_date_filter_combo(paging_db):
    after = ANCHOR - timedelta(days=365)
    q = PermitQuery(jurisdiction=JURISDICTION, status=["issued", "COMPLETED"], issued_after=after,
                    min_cost=50_000, permit_class="residential", page_size=200)
    with Session(paging_db) as s:
        repo = PermitRepository(s)
        got, cursor = [], None
        while True:
            q.cursor = cursor
            page = repo.list(q)
            got.extend(p.id for p in page.rows)
            cursor = page.next_cursor
            if not cursor:
                break
        expected = {r[0] for r in s.execute(text(
            "SELECT id FROM permits WHERE status IN ('ISSUED','COMPLETED') AND issued_at >= :a "
            "AND valuation >= 50000 AND permit_class_mapped = 'Residential'"), {"a": after})}
        estimate = repo.estimate(q)
    assert set(got) == expected and len(got) == len(expected) > 0
    assert estimate > 0


def test_get_by_number(paging_db):
    with Session(paging_db) as s:
        number = s.execute(text("SELECT permit_number FROM permits LIMIT 1")).scalar()
        repo = PermitRepository(s)
        assert repo.get(JURISDICTION, number).permit_number == number
        assert repo.get(JURISDICTION, "NOPE") is None

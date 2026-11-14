"""PermitQuery.trade: matches the latest non-REJECTED skill inference, case-insensitively,
honours trade_min_confidence, and reports coverage (permits it cannot see)."""

import pytest
from conftest import JURISDICTION, copy_rows, make_permit_rows
from sqlalchemy import text
from sqlalchemy.orm import Session

from permit_trade_skills.store.query import PermitQuery
from permit_trade_skills.store.repos import InferenceRepository, PermitRepository


def _skill(trade, confidence="high"):
    return {"skill": f"{trade.lower()} work skill", "trade": trade, "evidence": "x",
            "evidence_source": "https://example.test", "confidence": confidence}


@pytest.fixture
def trade_db(clean_db, db_url):
    """50 permits; six carry inferences (the rest have none)."""
    copy_rows(db_url, make_permit_rows(50, seed=5))
    with Session(clean_db) as s:
        ids = [r[0] for r in s.execute(text("SELECT id FROM permits ORDER BY id LIMIT 6"))]
        inf = InferenceRepository(s)
        inf.upsert_inference(ids[0], "v1", "m", [_skill("Carpentry"), _skill("HVAC", "medium")])
        inf.upsert_inference(ids[1], "v1", "m", [_skill("electrical", "low")])
        # superseded: the older inference said Electrical, the latest says Plumbing only
        inf.upsert_inference(ids[2], "v1", "m", [_skill("Electrical")])
        s.flush()
        s.execute(text("UPDATE skill_inferences SET created_at = now() - interval '1 day' "
                       "WHERE permit_id = :p"), {"p": ids[2]})
        inf.upsert_inference(ids[2], "v1", "m", [_skill("Plumbing")])
        # rejected by a reviewer: never matches
        rejected = inf.upsert_inference(ids[3], "v1", "m", [_skill("Carpentry")])
        s.execute(text("UPDATE skill_inferences SET review_status = 'REJECTED' WHERE id = :i"),
                  {"i": rejected})
        inf.upsert_inference(ids[4], "v1", "m", [_skill("Roofing")])
        inf.upsert_inference(ids[5], "v1", "m", [_skill("Carpentry", "low"), _skill("Electrical")])
        s.commit()
    return clean_db, ids


def _match(session, **kw):
    page = PermitRepository(session).list(PermitQuery(jurisdiction=JURISDICTION, page_size=200, **kw))
    return {p.id for p in page.rows}


def test_trade_matches_latest_usable_inference(trade_db):
    engine, ids = trade_db
    with Session(engine) as s:
        assert _match(s, trade="carpentry") == {ids[0], ids[5]}            # case-insensitive; not rejected
        assert _match(s, trade="Electrical") == {ids[1], ids[5]}           # not the superseded inference
        assert _match(s, trade="Plumbing") == {ids[2]}
        assert _match(s, trade=["Roofing", "HVAC"]) == {ids[0], ids[4]}    # any of
        assert _match(s, trade="Concrete") == set()


def test_trade_match_all_needs_every_trade(trade_db):
    engine, ids = trade_db
    with Session(engine) as s:
        assert _match(s, trade=["Carpentry", "HVAC"], trade_match="all") == {ids[0]}
        assert _match(s, trade=["Carpentry", "Electrical"], trade_match="all") == {ids[5]}
        assert _match(s, trade=["Carpentry", "Electrical"], trade_match="any") == {ids[0], ids[1], ids[5]}
        # every trade must meet the minimum: ids[5]'s Carpentry is low
        assert _match(s, trade=["Carpentry", "Electrical"], trade_match="all",
                      trade_min_confidence="medium") == set()
        # superseded inference does not count towards "all"
        assert _match(s, trade=["Plumbing", "Electrical"], trade_match="all") == set()
        q = PermitQuery(jurisdiction=JURISDICTION, trade=["Carpentry", "HVAC"], trade_match="all")
        assert PermitRepository(s).trade_coverage(q) == (50, 5, 1)


def test_trade_confidences_per_permit(trade_db):
    engine, ids = trade_db
    with Session(engine) as s:
        got = PermitRepository(s).trade_confidences(ids, ["carpentry", "Electrical", "HVAC"])
    assert got[ids[0]] == {"carpentry": "high", "HVAC": "medium"}
    assert got[ids[1]] == {"Electrical": "low"}
    assert got[ids[2]] == {}                          # latest inference has Plumbing only
    assert ids[3] not in got                          # REJECTED
    assert got[ids[5]] == {"carpentry": "low", "Electrical": "high"}


def test_trade_min_confidence(trade_db):
    engine, ids = trade_db
    with Session(engine) as s:
        assert _match(s, trade="Carpentry", trade_min_confidence="medium") == {ids[0]}
        assert _match(s, trade="HVAC", trade_min_confidence="medium") == {ids[0]}
        assert _match(s, trade="HVAC", trade_min_confidence="high") == set()
        assert _match(s, trade="Electrical", trade_min_confidence="low") == {ids[1], ids[5]}


def test_trade_combines_with_other_filters_and_reports_coverage(trade_db):
    engine, ids = trade_db
    with Session(engine) as s:
        repo = PermitRepository(s)
        q = PermitQuery(jurisdiction=JURISDICTION, trade="Carpentry")
        # 50 candidates; 5 usable inferences (ids[3]'s latest is REJECTED); 2 matches
        assert repo.trade_coverage(q) == (50, 5, 2)
        status = s.execute(text("SELECT status FROM permits WHERE id = :i"), {"i": ids[0]}).scalar()
        q = PermitQuery(jurisdiction=JURISDICTION, trade="Carpentry", status=status, page_size=200)
        assert ids[0] in _match(s, trade="Carpentry", status=status)
        candidates, inferred, matched = repo.trade_coverage(q)
        assert matched == len(repo.list(q).rows) and matched <= inferred <= candidates < 50


def test_distinct_trades(trade_db):
    engine, _ = trade_db
    with Session(engine) as s:
        trades = PermitRepository(s).distinct_trades([JURISDICTION])
    assert {"Carpentry", "Electrical", "HVAC", "Plumbing", "Roofing"} <= set(trades)

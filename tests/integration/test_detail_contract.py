"""Contract: the exact DetailDocument shape `DetailService.ensure_fresh()` returns to
Stage 06 (skills) and Stage 07 (the orchestrator's fetch_detail tool)."""

from datetime import datetime

import pytest

from conftest import TEST_DB_URL
from detail_fakes import TEST_SETTINGS, detail_url, portal_ok, seed_jurisdiction, seed_permit
from permit_trade_skills.detail import DETAIL_DOCUMENT_KEYS, DetailDocument, DetailService, DetailUnavailable
from permit_trade_skills.detail.fetchers import ScriptedFetcher
from permit_trade_skills.detail.models import REDIRECT_CHASE
from permit_trade_skills.store.details import SqlDetailStore
from permit_trade_skills.wiring import detail_service

EXPECTED_KEYS = ("permit_number", "jurisdiction", "snapshot_id", "final_url", "content_text", "fields",
                 "fetch_strategy", "fetched_at", "content_hash", "degraded", "notes", "signals")
SIGNAL_KEYS = {"count", "min_signals", "anchor", "passed", "matched", "details"}


def test_ensure_fresh_return_shape(clean_db):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    svc = DetailService(SqlDetailStore(TEST_DB_URL),
                        ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok()}),
                        settings=TEST_SETTINGS)
    doc = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert isinstance(doc, DetailDocument)
    d = doc.to_dict()
    assert tuple(d) == EXPECTED_KEYS == DETAIL_DOCUMENT_KEYS
    assert isinstance(d["snapshot_id"], int) and isinstance(d["content_text"], str)
    assert d["fetch_strategy"] in ("soda_row", "redirect_chase", "playwright")
    assert isinstance(d["degraded"], bool) and isinstance(d["notes"], list)
    assert set(d["signals"]) == SIGNAL_KEYS and d["signals"]["passed"] is True
    assert set(d["fields"]) >= {"catalog", "portal", "portal_pairs", "attachments"}
    assert len(d["content_hash"]) == 64
    datetime.fromisoformat(d["fetched_at"].rstrip("Z"))
    assert d["content_text"].rstrip().splitlines()[-1] == f"SOURCE: {d['final_url']}"
    # the stored snapshot round-trips to the same document
    again = svc.latest("7083363-CN", "seattle-kingcounty-wa").to_dict()
    assert {k: again[k] for k in EXPECTED_KEYS if k != "fetched_at"} == \
        {k: d[k] for k in EXPECTED_KEYS if k != "fetched_at"}


def test_unavailable_contract(clean_db):
    seed_jurisdiction(clean_db, plan="redirect_chase")
    seed_permit(clean_db)
    svc = DetailService(SqlDetailStore(TEST_DB_URL), ScriptedFetcher(default=None), settings={
        **TEST_SETTINGS, "retries": 0})
    with pytest.raises(DetailUnavailable) as exc:
        svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert exc.value.error and isinstance(exc.value.retryable, bool) and exc.value.outcome is not None


def test_wiring_builds_a_service():
    svc = detail_service(TEST_DB_URL, fetcher=ScriptedFetcher(), settings=TEST_SETTINGS)
    assert isinstance(svc, DetailService) and isinstance(svc.store, SqlDetailStore)

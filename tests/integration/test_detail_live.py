"""Live E2E (opt-in, nightly): fetch the detail record of a currently ISSUED Seattle permit.

    PTS_DETAIL_LIVE=1 uv run pytest tests/integration/test_detail_live.py -s

Pulls the most recently issued permits from the live Socrata API into the test
database, picks one through the Stage 04 query API (`pick_live_permits`, as
`pts detail pick-live` does), fetches it with the production fetchers
(no browser: Seattle is redirect_chase), checks the Stage 05 acceptance
properties and writes the evidence to evals/evidence/seattle/detail/.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, text

from conftest import TEST_DB_URL
from detail_fakes import FIELD_MAP, JURISDICTION, seed_permit
from permit_trade_skills.cli.seeds import SEATTLE_DETAIL
from permit_trade_skills.detail.live import pick_live_permits
from permit_trade_skills.detail.service import DetailService
from permit_trade_skills.detail.signals import missing_citations
from permit_trade_skills.query.service import QueryService
from permit_trade_skills.settings import load_settings
from permit_trade_skills.store.details import SqlDetailStore

LIVE = os.environ.get("PTS_DETAIL_LIVE") == "1"
EVIDENCE = Path(__file__).resolve().parents[2] / "evals" / "evidence" / "seattle" / "detail"

pytestmark = pytest.mark.skipif(not LIVE, reason="live detail E2E: set PTS_DETAIL_LIVE=1")


def _live_issued_rows(n=5):
    url = SEATTLE_DETAIL["soda_resource_url"]
    params = {"$where": "statuscurrent='Issued' AND issueddate IS NOT NULL", "$order": "issueddate DESC",
              "$limit": n}
    resp = httpx.get(url, params=params, timeout=60, headers={"User-Agent": "PermitTradeSkills-live-test"})
    resp.raise_for_status()
    return resp.json()


def test_live_issued_seattle_permit(clean_db: Engine):
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE jurisdictions SET base_url = 'https://cos-data.seattle.gov', "
                          "dataset_id = '76t5-zqzr', field_map = :f, detail_strategy = :s WHERE id = :id"),
                     {"f": json.dumps(FIELD_MAP), "s": json.dumps({"detail": SEATTLE_DETAIL}), "id": JURISDICTION})
    for row in _live_issued_rows():
        seed_permit(clean_db, row)

    number = pick_live_permits(QueryService(TEST_DB_URL), JURISDICTION, 1)[0]
    svc = DetailService(SqlDetailStore(TEST_DB_URL), settings=load_settings()["detail"])
    doc = svc.ensure_fresh(number, JURISDICTION, force=True)
    permit = svc._permit(JURISDICTION, number)

    assert doc.fetch_strategy in ("redirect_chase", "soda_row")
    assert doc.signals["count"] >= 4 and doc.signals["passed"]
    assert number in doc.content_text and permit.address in doc.content_text
    assert missing_citations(doc.content_text, permit) == []

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    evidence = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "test": "tests/integration/test_detail_live.py",
        "permit_number": number,
        "catalog": {"address": permit.address, "description": permit.description,
                    "issued_at": permit.issued_at.isoformat() if permit.issued_at else None,
                    "detail_url": permit.detail_url},
        "document": doc.to_dict(),
        "missing_citations": [],
    }
    path = EVIDENCE / f"{stamp}-{number}.json"
    path.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
    print(f"\nlive detail: {number} via {doc.fetch_strategy}, signals {doc.signals['count']}/"
          f"{doc.signals['min_signals']}, evidence {path.relative_to(EVIDENCE.parents[3])}")

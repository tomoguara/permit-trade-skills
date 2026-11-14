"""Shared helpers for the Stage 05 detail tests: fixtures, config, permits, DB seeding."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text

from permit_trade_skills.detail.config import load_config
from permit_trade_skills.detail.models import CapturedResponse, FetchResult, PermitRef
from permit_trade_skills.ingest.normalizers import compute_raw_hash

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "portals"
JURISDICTION = "seattle-kingcounty-wa"
PORTAL_HOST = "services.seattle.gov"
SODA_URL = "https://soda.example.gov/resource/abcd-1234.json"
NUMBER = "7083363-CN"

FIELD_MAP = {
    "permit_num": "permitnum", "status_current": "statuscurrent", "description": "description",
    "address": "originaladdress1", "city": "originalcity", "state": "originalstate", "zip_code": "originalzip",
    "issue_date": "issueddate", "applied_at": "applieddate", "expires_at": "expiresdate",
    "valuation": "estprojectcost", "permit_class": "permitclass", "permit_class_mapped": "permitclassmapped",
    "permit_type_mapped": "permittypemapped", "permit_type_desc": "permittypedesc",
}

DETAIL_BLOCK = {
    "fetch_strategy": "redirect_chase_then_soda",
    "allowed_hosts": [PORTAL_HOST],
    "soda_resource_url": SODA_URL,
    "soda_key_column": "permitnum",
    "min_signals": 4,
    "field_order": [["PERMIT", "permitnum"], ["STATUS", "statuscurrent"], ["ADDRESS", "originaladdress1"],
                    ["ISSUED", "issueddate"], ["APPLIED", "applieddate"], ["DESCRIPTION", "description"],
                    ["CLASSIFICATION", "permitclass"]],
    "portal": {"include": [".rec-left", "#tab-record_detail"],
               "field_labels": {"Total Issued Value": "valuation"}},
}

# Settings for tests: no waiting, no rate limiting, judge off.
TEST_SETTINGS: Dict[str, Any] = {
    "judge_enabled": False, "min_snapshot_age_hours": 24, "concurrency": 3, "max_concurrency": 8,
    "user_agent": "PermitTradeSkills-test", "nav_timeout_s": 20, "settle_timeout_s": 8,
    "total_budget_s": 60, "retries": 2, "backoff_s": [1, 3], "max_retry_after_s": 30,
    "rate_limit_per_minute": 0, "human_gate_cooldown_s": 3600, "cache_size": 512,
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def soda_row(**overrides) -> Dict[str, Any]:
    row = json.loads(fixture("soda_row.json"))[0]
    row.update(overrides)
    return row


def jurisdiction_dict(plan: Optional[str] = None, **block_overrides) -> Dict[str, Any]:
    block = copy.deepcopy(DETAIL_BLOCK)
    if plan:
        block["fetch_strategy"] = plan
    block.update(block_overrides)
    return {"id": JURISDICTION, "platform_type": "socrata", "base_url": "https://soda.example.gov",
            "dataset_id": "abcd-1234", "field_map": FIELD_MAP, "detail_strategy": {"detail": block}}


def config(plan: Optional[str] = None, **kw):
    return load_config(jurisdiction_dict(plan, **kw))


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value[:19]) if value else None


def permit_ref(row: Optional[Dict[str, Any]] = None, permit_id: int = 1) -> PermitRef:
    row = row or soda_row()
    return PermitRef(
        permit_id=permit_id, jurisdiction=JURISDICTION, permit_number=row["permitnum"],
        detail_url=f"https://{PORTAL_HOST}/portal/customize/LinkToRecord.aspx?altId={row['permitnum']}",
        raw_row=row, raw_hash=compute_raw_hash(row), status="ISSUED", address=row.get("originaladdress1"),
        description=row.get("description"), issued_at=_dt(row.get("issueddate")),
        applied_at=_dt(row.get("applieddate")), expires_at=_dt(row.get("expiresdate")),
    )


def detail_url(number: str = NUMBER) -> str:
    return f"https://{PORTAL_HOST}/portal/customize/LinkToRecord.aspx?altId={number}"


FINAL_URL = f"https://{PORTAL_HOST}/Portal/cap/CapDetail.aspx?capID1=25SCI&capID3=45424"


def portal_ok(html: Optional[str] = None, final_url: str = FINAL_URL, responses=()) -> FetchResult:
    return FetchResult(ok=True, final_url=final_url, http_status=200,
                       html=html if html is not None else fixture("ssr.html"), responses=list(responses))


def soda_ok(row: Optional[Dict[str, Any]] = None, url: str = SODA_URL) -> FetchResult:
    body = json.dumps([row or soda_row()])
    return FetchResult(ok=True, final_url=url, http_status=200, responses=[CapturedResponse(url, "json", body)])


# ---- DB seeding (integration tests) -------------------------------------------


def seed_jurisdiction(engine, plan: Optional[str] = None, **block_overrides) -> None:
    j = jurisdiction_dict(plan, **block_overrides)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jurisdictions SET base_url = :b, dataset_id = :d, field_map = :f, "
                          "detail_strategy = :s WHERE id = :id"),
                     {"b": j["base_url"], "d": j["dataset_id"], "f": json.dumps(j["field_map"]),
                      "s": json.dumps(j["detail_strategy"]), "id": JURISDICTION})


def seed_permit(engine, row: Optional[Dict[str, Any]] = None, *, status: str = "ISSUED",
                state: str = "SYNCED") -> int:
    row = row or soda_row()
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO permits (jurisdiction_id, jurisdiction_slug, permit_number, status, status_raw, "
            "description, address, issued_at, applied_at, expires_at, detail_url, raw_row, raw_hash, run_id, "
            "created_at, updated_at, processing_state) VALUES (:j, :j, :n, :st, :raw_status, :d, :a, :i, :ap, "
            ":e, :u, :row, :h, 'test', now(), now(), :state) RETURNING id"),
            {"j": JURISDICTION, "n": row["permitnum"], "st": status, "raw_status": row.get("statuscurrent"),
             "d": row.get("description"), "a": row.get("originaladdress1"), "i": _dt(row.get("issueddate")),
             "ap": _dt(row.get("applieddate")), "e": _dt(row.get("expiresdate")),
             "u": detail_url(row["permitnum"]), "row": json.dumps(row), "h": compute_raw_hash(row),
             "state": state}).scalar_one()


def update_catalog_row(engine, permit_id: int, row: Dict[str, Any]) -> None:
    """Simulate `pts sync` picking up a changed source row."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE permits SET raw_row = :row, raw_hash = :h, status_raw = :s WHERE id = :id"),
                     {"row": json.dumps(row), "h": compute_raw_hash(row), "s": row.get("statuscurrent"),
                      "id": permit_id})

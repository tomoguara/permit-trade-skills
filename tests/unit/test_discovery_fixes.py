"""Discovery: catalog candidates (B1), local-file map guessing (B2), dossier -> jurisdiction (B10)."""

import asyncio

from permit_trade_skills.cli.seeds import SEATTLE_FIELD_MAP, SEATTLE_STATUS_MAP
from permit_trade_skills.discovery import DiscoveryEngine
from permit_trade_skills.discovery.catalog_client import CatalogClient
from permit_trade_skills.discovery.mapping import guess_field_map, guess_status_map, sample_columns
from permit_trade_skills.discovery.schemas import (
    DiscoveryCandidate, DiscoveryDossier, DiscoveryStatus, PlatformType,
)
from permit_trade_skills.discovery.suggest import Scorer

SEATTLE_STATUSES = [v for values in SEATTLE_STATUS_MAP.values() for v in values]


def test_catalog_results_become_candidates(monkeypatch):
    packages = [
        {"id": "p1", "title": "City of Seattle Building Permits", "organization": {"title": "City of Seattle"},
         "url": "https://data.seattle.gov/d/76t5-zqzr", "tags": [{"name": "permits"}], "notes": "x"},
        {"id": "p2", "title": "Parks", "organization": {}, "url": "https://data.example.gov/parks"},
    ]

    async def fake_search(self, name, state, limit, queries=None):
        return packages

    monkeypatch.setattr(CatalogClient, "search_by_jurisdiction", fake_search)
    run, candidates = asyncio.run(CatalogClient().discovery_run("Seattle", "Seattle", "WA"))
    assert run.status == DiscoveryStatus.COMPLETED and run.error_message is None
    assert [c.id for c in candidates] == ["p1", "p2"]
    assert all(c.platform_type is None for c in candidates)
    ranked = Scorer("Seattle", "WA").score_and_rank(candidates)
    assert ranked[0].id == "p1"


def test_status_rules_reproduce_the_seattle_seed():
    status_map, unmatched = guess_status_map(SEATTLE_STATUSES + ["Issued", "", None])
    assert unmatched == []
    assert {k: set(v) for k, v in status_map.items()} == {k: set(v) for k, v in SEATTLE_STATUS_MAP.items()}


def test_unknown_status_values_are_left_out():
    status_map, unmatched = guess_status_map(["Issued", "Mystery State"])
    assert status_map == {"issued": ["Issued"]} and unmatched == ["Mystery State"]


def test_field_map_from_columns_across_records():
    # Socrata omits null fields per row: issueddate only appears in the second record.
    records = [{":id": "row-1", "permitnum": "1", "statuscurrent": "Issued", "estprojectcost": "10"},
               {"permitnum": "2", "issueddate": "2026-01-01", "contractorcompanyname": "ACME"}]
    columns = sample_columns(records)
    assert ":id" not in columns and "issueddate" in columns
    field_map = guess_field_map(columns)
    assert field_map["issue_date"] == "issueddate"
    assert field_map["valuation"] == "estprojectcost"
    assert field_map["contractor_name"] == "contractorcompanyname"


def test_field_map_matches_seed_for_seattle_columns():
    field_map = guess_field_map(SEATTLE_FIELD_MAP.values())
    assert field_map == SEATTLE_FIELD_MAP


def test_jurisdiction_from_dossier_uses_dossier_maps_and_defaults_platform():
    candidate = DiscoveryCandidate(id="d1", title="T", organization="O", url="https://x.gov",
                                   platform_detected_by="", confidence=0.5)
    dossier = DiscoveryDossier(
        jurisdiction_id="town-or", jurisdiction_name="Town", candidates=[candidate],
        verification_results=[], selected_candidate=candidate,
        field_mapping={"permit_num": "permitnum"}, status_mapping={"issued": ["Issued"]},
        detail_strategy={"timestamp_field": "issueddate"},
    )
    j = DiscoveryEngine.jurisdiction_from_dossier(dossier, "Town", "OR", "Lane")
    assert j.platform_type == PlatformType.OTHER
    assert (j.field_map, j.status_map, j.detail_strategy) == (
        {"permit_num": "permitnum"}, {"issued": ["Issued"]}, {"timestamp_field": "issueddate"})
    assert (j.id, j.dataset_id, j.api_endpoint) == ("town-or", "d1", "https://x.gov")

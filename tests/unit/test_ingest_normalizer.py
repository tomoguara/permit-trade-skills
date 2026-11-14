"""PermitNormalizer: detail URL rule and jurisdiction defaults (B9), plus no hardcoded IDs (Stage 02 AC3)."""

from pathlib import Path

import pytest

from permit_trade_skills.ingest.normalizers import PermitNormalizer, render_detail_url_rule

INGEST_DIR = Path(__file__).resolve().parents[2] / "src" / "permit_trade_skills" / "ingest"
RULE = "https://portal.example.gov/record?altId={{permitnum}}"


def normalizer(detail_strategy=None, default_state="OR"):
    return PermitNormalizer(
        jurisdiction_id="testville-county-or", jurisdiction_slug="testville-county-or",
        field_map={"permit_num": "permitnum", "status_current": "statuscurrent",
                   "city": "originalcity", "state": "originalstate"},
        status_map={"issued": ["Issued"]},
        detail_strategy=detail_strategy or {},
        default_state=default_state,
    )


def test_link_object_wins_over_rule():
    p = normalizer({"detail_url_rule": RULE}).normalize(
        {"permitnum": "1", "statuscurrent": "Issued", "link": {"url": "https://x.gov/1"}}, "run")
    assert p["detail_url"] == "https://x.gov/1"
    assert {"type": "missing_detail_url"} not in p["warnings"]


def test_rule_renders_and_encodes_when_no_link():
    p = normalizer({"detail_url_rule": RULE}).normalize({"permitnum": "12 34/A", "statuscurrent": "Issued"}, "run")
    assert p["detail_url"] == "https://portal.example.gov/record?altId=12%2034%2FA"


def test_no_link_no_rule_is_null_with_warning():
    p = normalizer({"type": "dataset_api"}).normalize({"permitnum": "1", "statuscurrent": "Issued"}, "run")
    assert p["detail_url"] is None
    assert {"type": "missing_detail_url"} in p["warnings"]


def test_rule_with_empty_placeholder_is_none():
    assert render_detail_url_rule(RULE, {"permitnum": ""}) is None
    assert render_detail_url_rule("https://x/{{ a }}/{{b}}", {"a": "1", "b": "2"}) == "https://x/1/2"


def test_state_defaults_to_jurisdiction_and_city_has_no_default():
    p = normalizer().normalize({"permitnum": "1", "statuscurrent": "Issued"}, "run")
    assert p["city"] is None and p["state"] == "OR"
    p = normalizer().normalize({"permitnum": "1", "originalcity": "Bend", "originalstate": "WA"}, "run")
    assert (p["city"], p["state"]) == ("Bend", "WA")


@pytest.mark.parametrize("needle", ["76t5-zqzr", "cos-data.seattle.gov", "services.seattle.gov", "'Seattle'"])
def test_ingest_has_no_jurisdiction_specific_values(needle):
    hits = [f"{path.name}:{i}" for path in INGEST_DIR.glob("*.py")
            for i, line in enumerate(path.read_text().splitlines(), 1) if needle in line]
    assert not hits, f"{needle!r} hardcoded in ingest/: {hits}"


# ---- Stage 02 N1/N2: typed record, coercion rules, whitespace, provenance ----

from datetime import datetime  # noqa: E402

from permit_trade_skills.ingest.normalizers import compute_raw_hash  # noqa: E402
from permit_trade_skills.ingest.schemas import PermitRecord  # noqa: E402

FULL_MAP = {"permit_num": "permitnum", "status_current": "statuscurrent", "issue_date": "issueddate",
            "applied_at": "applieddate", "valuation": "estprojectcost", "housing_units": "housingunits",
            "latitude": "latitude", "longitude": "longitude", "description": "description",
            "contractor_name": "contractorcompanyname"}


def full_normalizer(status_map=None):
    return PermitNormalizer("j", "j", FULL_MAP, status_map or {"issued": ["Issued"], "bogus": ["Odd"]},
                            {"detail_url_rule": RULE}, default_state="WA")


def norm(**row):
    return full_normalizer().normalize_record({"permitnum": "P-1", "statuscurrent": "Issued", **row}, "run")


def kinds(record):
    return [w["type"] for w in record.warnings]


def test_normalize_record_is_typed_and_row_is_plain():
    record = norm()
    assert isinstance(record, PermitRecord)
    row = record.to_row()
    assert row["status"] == "ISSUED" and row["processing_state"] == "SYNCED"
    assert row["state"] == "WA"


@pytest.mark.parametrize("value, expected", [
    ("2025-11-03T00:00:00.000", datetime(2025, 11, 3)),
    ("2025-11-03", datetime(2025, 11, 3)),
    ("2025-11-03T10:20:30Z", datetime(2025, 11, 3, 10, 20, 30)),
])
def test_date_formats(value, expected):
    assert norm(issueddate=value).issue_date == expected


def test_unparseable_date_is_null_with_warning():
    record = norm(issueddate="not a date", applieddate="")
    assert record.issue_date is None and record.applied_at is None
    assert {"type": "unparseable_date", "field": "issue_date", "value": "not a date"} in record.warnings
    assert kinds(record).count("unparseable_date") == 1  # empty strings are just missing


def test_cost_rules():
    assert norm(estprojectcost="25000.0000").valuation == 25000.0
    assert norm(estprojectcost="1,250.50").valuation == 1250.5
    assert norm(estprojectcost="0.0").valuation == 0.0          # zero is kept, distinct from null
    assert norm().valuation is None and "unparseable_number" not in kinds(norm())
    bad = norm(estprojectcost="TBD")
    assert bad.valuation is None and {"type": "unparseable_number", "field": "valuation",
                                      "value": "TBD"} in bad.warnings


def test_integers_accept_float_strings():
    assert norm(housingunits="1.0").housing_units == 1
    assert "unparseable_number" in kinds(norm(housingunits="one"))


def test_coordinates_out_of_range_are_dropped_with_warning():
    record = norm(latitude="47.6", longitude="-200")
    assert (record.latitude, record.longitude) == (None, None)
    assert "invalid_coordinates" in kinds(record)


def test_text_is_trimmed_and_collapsed_but_status_raw_kept():
    record = full_normalizer().normalize_record(
        {"permitnum": " P-2 ", "statuscurrent": " Issued ", "description": "  Replace\n  roof   and gutters ",
         "contractorcompanyname": "ACME  Roofing"}, "run")
    assert record.permit_number == "P-2"
    assert record.description == "Replace roof and gutters"
    assert record.contractor_name == "ACME Roofing"
    assert record.status_raw == " Issued " and record.status == "ISSUED"  # verbatim, matched trimmed


def test_whitespace_only_text_is_null():
    assert norm(description="   ").description is None


def test_status_map_unknown_and_non_canonical_keys():
    assert norm(statuscurrent="ISSUED").status == "ISSUED"           # case-insensitive
    unknown = norm(statuscurrent="Mystery")
    assert unknown.status == "UNKNOWN" and {"type": "unknown_status", "raw": "Mystery"} in unknown.warnings
    assert norm(statuscurrent="Odd").status == "UNKNOWN"             # key 'bogus' isn't canonical


def test_source_updated_at_precedence():
    assert norm(**{":updated_at": "2026-09-26T01:11:06.798Z"}).source_updated_at == \
        datetime(2026, 9, 26, 1, 11, 6, 798000)
    assert norm(issueddate="2026-01-02").source_updated_at == datetime(2026, 1, 2)
    missing = norm()
    assert missing.source_updated_at is None and "missing_source_updated_at" in kinds(missing)


def test_raw_row_is_verbatim_and_hash_ignores_system_fields():
    raw = {":id": "row-1", ":updated_at": "2026-01-01T00:00:00Z", "permitnum": "P-1", "description": " x  y "}
    record = full_normalizer().normalize_record(raw, "run")
    stored = record.to_row()["raw_row"]
    assert stored == raw and list(stored) == list(raw)  # verbatim, key order kept
    assert record.raw_hash == compute_raw_hash({"permitnum": "P-1", "description": " x  y "})
    assert record.row_id_source == "row-1"

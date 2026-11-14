"""Stage 05 F2: deterministic relevance signals (pass / fail / threshold edges, wrong permit)."""

from dataclasses import replace

import pytest

from detail_fakes import config, fixture, permit_ref, soda_row
from permit_trade_skills.detail.normalize import Extraction, extract_portal, extract_row
from permit_trade_skills.detail.signals import (
    address_tokens,
    content_words,
    dates_in,
    missing_citations,
    score,
)

DESC = "Construct alterations to the basement of an existing one family dwelling, per plan."


def _cand(**mapped) -> Extraction:
    pairs = [(k, v) for k, v in mapped.items()]
    return Extraction(source="dom", pairs=pairs, mapped=dict(mapped))


def test_ssr_page_passes_with_all_five_signals():
    report = score(extract_portal(fixture("ssr.html"), [], config()), permit_ref())
    assert report.passed and report.count == 5
    assert report.matched == {"anchor": True, "address": True, "date": True, "description": True, "other": True}


def test_soda_row_passes():
    report = score(extract_row(soda_row(), config()), permit_ref())
    assert report.passed and report.count == 5


def test_wrong_permit_page_fails_the_anchor():
    """Same address, date and description, and it even mentions our number: still another permit."""
    report = score(extract_portal(fixture("wrong_permit.html"), [], config()), permit_ref())
    assert report.matched["address"] and report.matched["date"] and report.matched["description"]
    assert report.anchor is False
    assert report.count == 4 and not report.passed


def test_anchor_from_text_when_no_record_number_field():
    cand = Extraction(source="dom", pairs=[("Address", "3010 E CHERRY ST")], loose=["Permit 7083363-CN details"],
                      mapped={"address": "3010 E CHERRY ST"})
    assert score(cand, permit_ref()).anchor
    cand.loose = ["Permit 7083364-CN details"]
    assert not score(cand, permit_ref()).anchor


def test_record_number_comparison_ignores_punctuation_and_case():
    assert score(_cand(permit_number="7083363cn"), permit_ref()).anchor


@pytest.mark.parametrize("n_signals,passes", [(3, False), (4, True), (5, True)])
def test_threshold_edges(n_signals, passes):
    mapped = {"permit_number": "7083363-CN"}                       # anchor
    if n_signals >= 2:
        mapped["address"] = "3010 E CHERRY ST"
    if n_signals >= 3:
        mapped["issued_date"] = "01/15/2026"
    if n_signals >= 4:
        mapped["description"] = DESC
    if n_signals >= 5:
        mapped["valuation"] = "25000"
    report = score(_cand(**mapped), permit_ref())
    assert report.count == n_signals and report.passed is passes


def test_anchor_is_required_even_with_four_other_signals():
    cand = _cand(permit_number="1111111-XX", address="3010 E CHERRY ST", issued_date="2026-01-16",
                 description=DESC, status="Issued")
    report = score(cand, permit_ref())
    assert report.count == 4 and not report.passed


def test_custom_min_signals():
    cand = _cand(permit_number="7083363-CN", address="3010 E CHERRY ST", issued_date="2026-01-15")
    assert score(cand, permit_ref(), min_signals=3).passed
    assert not score(cand, permit_ref(), min_signals=4).passed


def test_address_overlap_threshold():
    assert address_tokens("3010 E Cherry St.") == ["3010", "E", "CHERRY", "ST"]
    assert score(_cand(address="3010 E CHERRY ST, SEATTLE WA"), permit_ref()).matched["address"]
    # 3 of 4 tokens = 0.75 < 0.8
    assert not score(_cand(address="3010 CHERRY ST"), permit_ref()).matched["address"]


def test_date_within_one_day():
    assert dates_in("Issued 01/16/2026 and 2025-11-03") and len(dates_in("Issued 01/16/2026 and 2025-11-03")) == 2
    assert score(_cand(issued_date="01/16/2026"), permit_ref()).matched["date"]       # +1 day
    assert not score(_cand(issued_date="01/17/2026"), permit_ref()).matched["date"]   # +2 days
    assert not score(_cand(issued_date="13/45/2026"), permit_ref()).matched["date"]   # not a date


def test_description_overlap_needs_four_shared_words_and_jaccard():
    assert content_words("The new deck with a roof") == {"deck", "roof"}
    assert score(_cand(description=DESC), permit_ref()).matched["description"]
    # 3 shared content words -> fail
    assert not score(_cand(description="basement alterations dwelling"), permit_ref()).matched["description"]
    # 4 shared but diluted below Jaccard 0.3
    long = "basement alterations dwelling family " + " ".join(f"word{c}xx" for c in "abcdefghijklmnop")
    assert not score(_cand(description=long), permit_ref()).matched["description"]


def test_other_signal_from_tokens():
    cand = Extraction(source="dom", loose=["Inspection schedule follows"], mapped={})
    assert score(cand, permit_ref()).matched["other"]
    assert not score(Extraction(source="dom", loose=["nothing relevant here at all"]), permit_ref()).matched["other"]


def test_missing_citations():
    permit = permit_ref()
    assert missing_citations("PERMIT: 7083363-CN", permit) == [
        "3010 E CHERRY ST", permit.description, "2026-01-15"]
    no_address = replace(permit, address=None, description=None, issued_at=None)
    assert missing_citations("permit: 7083363-cn", no_address) == []

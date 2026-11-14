"""Stage 06 K2: the keyword -> trade signal map, strength, negative answers, the cross-check."""

import re

import pytest

from permit_trade_skills.skills import signals as S
from skills_fakes import load


def test_every_keyword_maps_to_a_trade_and_matches_itself():
    assert S.TRADE_SIGNALS, "signal map is empty"
    for trade, words in S.TRADE_SIGNALS.items():
        assert trade.strip() and words, f"{trade!r} has no keywords"
        for word in words:
            literal = re.sub(r"\??$", "", word.replace("s?", "s").replace("?", ""))
            assert S.line_trades(f"DESCRIPTION: {literal}") , f"{word!r} does not fire for {trade}"
            assert trade in S.line_trades(f"Scope: {literal} work"), f"{word!r} maps to another trade"


def test_overview_keywords_are_covered():
    # 06 K2's curated map (one example per trade)
    for word, trade in [("concrete", "Concrete/Foundation"), ("joists", "Framing/Carpentry"),
                        ("plaster", "Drywall"), ("wiring", "Electrical"), ("water heater", "Plumbing"),
                        ("mini-split", "HVAC"), ("shingle", "Roofing"), ("fiber cement", "Siding/Masonry"),
                        ("insulation", "Insulation"), ("primer", "Painting"), ("glazing", "Glazing/Carpentry"),
                        ("fireproofing", "Fireproofing/Spray Foam"), ("flue", "Sheet Metal/HVAC")]:
        assert trade in S.line_trades(f"x {word} y"), word


def test_scope_line_hits_are_high_others_low():
    found = S.detect("DESCRIPTION: Replace roof shingles and flashing\nOther Fire Protection System: Yes")
    assert found["Roofing"].strength == "high"
    assert found["Roofing"].keywords == ["flashing", "roof", "shingles"]
    assert found["Fireproofing/Spray Foam"].strength == "low"


def test_negative_form_answers_are_not_signals():
    text = "Fire Alarm: No\nIs there a spray paint booth?: No\nSprinkler Standard: None\nFloor(s): 0"
    assert S.detect(text) == {}
    assert S.is_negative_answer("Fire Alarm: No")
    assert not S.is_negative_answer("Fire Alarm: Yes")
    assert not S.is_negative_answer("DESCRIPTION: no change")  # free text, not a no-answer... value != "no"


def test_word_boundaries():
    assert S.line_trades("Stainless steel counters") == []        # not "stain"
    assert S.line_trades("Painted mural") == []                    # not "paint"


def test_tenant_improvement_signals_and_cross_check():
    fx = load("tenant_improvement")
    found = S.detect(fx["detail"]["content_text"])
    high = {t for t, s in found.items() if s.strength == "high"}
    assert {"Framing/Carpentry", "Drywall", "Electrical", "Plumbing", "HVAC", "Sheet Metal/HVAC"} <= high
    check = S.cross_check(found, fx["response"]["skills"])
    assert check["missing_high_signals"] == []
    # without the plumbing skill, Plumbing is a missing high signal
    skills = [s for s in fx["response"]["skills"] if s["trade"] != "Plumbing"]
    assert S.cross_check(found, skills)["missing_high_signals"] == ["Plumbing"]


def test_basement_has_no_high_signals():
    # "Construct alterations to the basement ..." names no trade keyword
    found = S.detect(load("basement")["detail"]["content_text"])
    assert all(s.strength == "low" for s in found.values())


@pytest.mark.parametrize("line,expected", [
    ("Project Description: Demolish existing structure(s)", True),
    ("Scope of work: new deck", True),
    ("DESCRIPTION: x", True),
    ("Work Location: 3010 E CHERRY ST", False),
    ("Fire Alarm: Yes", False),
])
def test_scope_lines(line, expected):
    assert S.is_scope_line(line) is expected

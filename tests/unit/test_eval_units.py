"""Stage 08 offline units: trade vocabulary, scoring, label hashing/validation, the golden loader,
KS test, percentile, judge sampling and guardrail config, gate criterion logic."""

from __future__ import annotations

import copy
import json
from datetime import date

import pytest

from permit_trade_skills.eval import labels as L
from permit_trade_skills.eval import trades as TR
from permit_trade_skills.eval.config import Threshold, thresholds
from permit_trade_skills.eval.drift import ks_two_sample
from permit_trade_skills.eval.harnesses import flatten, score_trades, unfulfilled_captured
from permit_trade_skills.eval.judge import JudgeConfigError, run_judge, stratified_sample
from permit_trade_skills.eval.slo import percentile

from eval_fakes import golden_trade_row


@pytest.mark.parametrize("trade,skill,expected", [
    ("Framing/Carpentry", "", {"Carpentry"}),
    ("Sheet Metal/HVAC", "", {"HVAC"}),
    ("Glazing/Carpentry", "", {"Windows/Doors", "Carpentry"}),
    ("Concrete/Foundation", "", {"Concrete"}),
    ("Earthwork", "", {"Excavation"}),
    ("Fireproofing", "", {"Fire Protection"}),
    ("General Construction", "", {"General Construction"}),
    ("Specialty", "Install new roof shingles", {"Roofing"}),
    ("Specialty", "Permit coordination", {"Other: Specialty"}),
])
def test_canonical_trades(trade, skill, expected):
    assert TR.canonical_trades(trade, skill) == expected


def test_canonical_name_and_unknown_labels():
    assert TR.canonical_name("hvac") == "HVAC"
    assert TR.canonical_name("Framing") == "Carpentry"
    assert TR.unknown_labels(["Carpentry", "Wizardry"]) == ["Wizardry"]


def test_score_trades_recall_precision_traps():
    label = {"required_trades": ["Carpentry", "Plumbing"], "optional_trades": ["Drywall"],
             "unrequired_trades": ["HVAC", "Roofing"]}
    s = score_trades({"Carpentry", "Drywall", "HVAC"}, label)
    assert s["recall"] == 0.5 and s["missing"] == ["Plumbing"]
    assert s["precision"] == round(2 / 3, 4) and s["extra"] == ["HVAC"]
    assert s["traps_claimed"] == ["HVAC"] and s["traps"] == 2
    assert score_trades(set(), label)["precision"] is None


def test_validate_payload_canonicalizes_and_rejects():
    p = L.validate_payload("trade_set", {"required_trades": ["framing", "Plumbing"], "unrequired_trades": ["hvac"],
                                         "defensible_skills": {"Carpentry": "Basement wall framing"}})
    assert p["required_trades"] == ["Carpentry", "Plumbing"] and p["unrequired_trades"] == ["HVAC"]
    assert p["defensible_skills"] == {"Carpentry": ["Basement wall framing"]}
    with pytest.raises(L.LabelError, match="not a canonical trade"):
        L.validate_payload("trade_set", {"required_trades": ["Wizardry"]})
    with pytest.raises(L.LabelError, match="overlaps"):
        L.validate_payload("trade_set", {"required_trades": ["Roofing"], "unrequired_trades": ["Roofing"]})
    with pytest.raises(L.LabelError, match="3 to 5"):
        L.validate_payload("extraction_checklist", {"must_appear": ["a", "b"]})


def test_trade_input_hash_binds_content(tmp_path):
    row = golden_trade_row("1-CN", ["Carpentry"])
    assert L.verify_trade_input(row["input"], row["pinned_input_hash"]) is None
    tampered = copy.deepcopy(row)
    tampered["input"]["snapshot"]["content_text"] += "\nInstall a rooftop helipad"
    assert "content_hash" in L.verify_trade_input(tampered["input"], row["pinned_input_hash"])
    relabeled = copy.deepcopy(row)
    relabeled["input"]["description"] = "something else"
    assert "pinned" in L.verify_trade_input(relabeled["input"], row["pinned_input_hash"])


def test_load_golden_marks_tampered_rows_superseded_ac1(tmp_path):
    good, bad = golden_trade_row("1-CN", ["Carpentry"]), golden_trade_row("2-CN", ["Plumbing"])
    bad["input"]["snapshot"]["content_text"] = bad["input"]["snapshot"]["content_text"].replace("kitchen", "garage")
    path = tmp_path / "trade-set.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in (good, bad)))
    rows = {r["id"]: r for r in L.load_golden(path, "trade_set")}
    assert rows["1-CN"]["status"] == L.ACTIVE
    assert rows["2-CN"]["status"] == L.SUPERSEDED and "content_hash" in rows["2-CN"]["status_reason"]


def test_nl_tokens_and_hash():
    assert L.resolve_token("@today-90d", date(2026, 3, 15)) == "2025-12-15"
    assert L.resolve_token("@last-month-start", date(2026, 3, 15)) == "2026-02-01"
    row = {"question": "q", "expected": {"status": ["ISSUED"]}, "as_of": "2026-11-10"}
    h = L.input_hash("nl_query", row)
    assert h == L.input_hash("nl_query", {**row, "gold_total": 7}) != L.input_hash("nl_query", {**row, "as_of": "x"})


def test_unfulfilled_capture():
    assert unfulfilled_captured(["Green Lake"], True, ["within 2 miles of Green Lake"])
    assert not unfulfilled_captured(["Green Lake"], True, [])
    assert unfulfilled_captured([], True, ["near the Space Needle"])
    assert unfulfilled_captured([], False, [])


def test_flatten_catalog_row():
    assert flatten({"permitnum": "1-CN", "link": {"url": "u"}, ":id": "x", "n": 3, "flag": True, "e": ""}) == \
        {"permitnum": "1-CN", "link.url": "u", "n": "3"}


def test_ks_two_sample():
    same = ks_two_sample({"high": 10, "medium": 10, "low": 10}, {"high": 10, "medium": 10, "low": 10})
    assert same == {"d": 0.0, "p": 1.0}
    shifted = ks_two_sample({"high": 2, "medium": 8, "low": 30}, {"high": 30, "medium": 8, "low": 2})
    assert shifted["d"] == 0.7 and shifted["p"] < 0.001


def test_percentile_linear():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.95) == 10
    assert percentile([], 0.95) is None


def test_stratified_sample_takes_30_percent_per_confidence():
    rows = [{"id": i, "overall_confidence": c} for i, c in enumerate(["high"] * 10 + ["medium"] * 7 + ["low"] * 3)]
    picked = stratified_sample(rows, 0.3)
    by = {c: sum(1 for r in picked if r["overall_confidence"] == c) for c in ("high", "medium", "low")}
    assert by == {"high": 3, "medium": 3, "low": 1}
    assert picked == stratified_sample(rows, 0.3)          # deterministic


def test_judge_refuses_to_grade_its_own_model():
    with pytest.raises(JudgeConfigError):
        run_judge(None, lambda: None, judge_model="ollama/qwen3.8:latest", generator_model="ollama/qwen3.8:latest")


def test_thresholds_come_from_config():
    ths = {t.criterion: t for t in thresholds({"eval": {"thresholds": {
        "H1.faithfulness": {"min": 1.0, "hard": True}, "H2.trap_hallucination_rate": {"max": 0.1}}}})}
    assert ths["H1.faithfulness"].hard and ths["H1.faithfulness"].passes(0.99) is False
    assert ths["H2.trap_hallucination_rate"].passes(0.1) is True and not ths["H2.trap_hallucination_rate"].hard
    assert Threshold("H5.fabrications", max=0).passes(None) is None


def test_repo_settings_define_every_gate_criterion_and_a_distinct_judge():
    from permit_trade_skills.settings import load_settings

    s = load_settings()
    names = {t.criterion for t in thresholds(s)}
    assert {"H1.faithfulness", "H2.recall", "H2.trap_hallucination_rate", "H3.full_match", "H4.must_appear",
            "H4.catalog_grounding", "H5.fabrications", "H6.loss", "H7.violations"} <= names
    for profile in s["llm"]["providers"].values():
        assert profile["models"]["eval.judge"] != profile["models"]["skills.infer"]

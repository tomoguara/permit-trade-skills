"""Stage 07 no-fabrication validator on three scripted transcripts: clean, one fabricated id, one unmet goal."""

from __future__ import annotations

from permit_trade_skills.orchestration.validator import (
    transcript_from_events,
    transcript_permits,
    validate_report,
)

SKILL = {"skill": "Interior partition framing", "trade": "Carpentry",
         "evidence": "Construct alterations to the basement", "confidence": "medium"}


def step(tool, params, data=None, ok=True, error_code=None):
    result = {"ok": True, "data": data} if ok else {"ok": False, "error_code": error_code, "retryable": False}
    return {"tool": tool, "params": params, "ok": ok, "result": result}


TRANSCRIPT = [
    step("list_jurisdictions", {}, {"jurisdictions": [{"slug": "j"}], "count": 1}),
    step("search_permits", {"status": ["ISSUED"]},
         {"rows": [{"permit_number": "A-1", "status": "ISSUED"}, {"permit_number": "B-2", "status": "ISSUED"}]}),
    step("ensure_detail", {"permit_number": "A-1"}, {"permit_number": "A-1", "snapshot_id": 1}),
    step("infer_trade_skills", {"permit_number": "A-1"},
         {"permit_number": "A-1", "skills": [{**SKILL, "evidence_source": "https://x"}]}),
    step("ensure_detail", {"permit_number": "B-2"}, ok=False, error_code="HUMAN_GATE"),
]


def report(findings, unfulfilled=(), citations=()):
    return {"findings": findings, "unfulfilled": list(unfulfilled), "citations": list(citations)}


def test_clean_transcript_passes():
    r = report([{"permit_number": "A-1", "status": "ISSUED", "detail_present": True, "skills": [SKILL]},
                {"permit_number": "B-2", "status": "ISSUED", "detail_present": False, "skills": []}],
               unfulfilled=[{"requested": "skills for B-2", "reason": "HUMAN_GATE", "suggested_action": "manual"}],
               citations=[{"claim_index": 0, "source": "tool:infer_trade_skills:A-1"},
                          {"claim_index": 1, "source": "tool:search_permits:B-2"}])
    out = validate_report(TRANSCRIPT, r)
    assert out == {"integrity": "PASSED", "violations": [], "warnings": []}


def test_whitespace_and_case_do_not_matter_for_skill_matching():
    loose = {**SKILL, "evidence": "  construct   alterations to the BASEMENT "}
    r = report([{"permit_number": "A-1", "status": "ISSUED", "detail_present": True, "skills": [loose]}])
    assert validate_report(TRANSCRIPT, r)["integrity"] == "PASSED"


def test_one_fabricated_id_fails():
    r = report([{"permit_number": "A-1", "status": "ISSUED", "detail_present": True, "skills": [SKILL]},
                {"permit_number": "Z-9", "status": "ISSUED", "detail_present": True, "skills": [SKILL]}])
    out = validate_report(TRANSCRIPT, r)
    assert out["integrity"] == "FAILED"
    assert [(v["type"], v["permit_number"]) for v in out["violations"]] == [("fabricated_permit", "Z-9")]


def test_permit_seen_only_in_a_detail_call_counts_as_fabricated():
    transcript = [step("ensure_detail", {"permit_number": "C-3"}, {"permit_number": "C-3"})]
    out = validate_report(transcript, report([{"permit_number": "C-3", "status": "ISSUED",
                                               "detail_present": True, "skills": []}]))
    assert out["violations"][0]["type"] == "fabricated_permit"


def test_invented_skill_fails():
    invented = {**SKILL, "skill": "Structural welding", "trade": "Welding"}
    out = validate_report(TRANSCRIPT, report([{"permit_number": "A-1", "status": "ISSUED",
                                               "detail_present": True, "skills": [invented]}]))
    assert [v["type"] for v in out["violations"]] == ["ungrounded_skill"]


def test_skill_from_another_permit_fails():
    out = validate_report(TRANSCRIPT, report([{"permit_number": "B-2", "status": "ISSUED",
                                               "detail_present": False, "skills": [SKILL]}]))
    assert [v["type"] for v in out["violations"]] == ["ungrounded_skill"]


def test_one_unmet_goal_passes_when_explained():
    r = report(None, unfulfilled=[{"requested": "permits over $10M", "reason": "search returned no rows",
                                   "suggested_action": "widen the cost filter"}])
    assert validate_report(TRANSCRIPT, r)["integrity"] == "PASSED"


def test_unmet_goal_without_explanation_fails():
    out = validate_report(TRANSCRIPT, report(None))
    assert [v["type"] for v in out["violations"]] == ["unexplained_gap"]


def test_bad_citations_are_warnings_only():
    r = report([{"permit_number": "A-1", "status": "ISSUED", "detail_present": True, "skills": []}],
               citations=[{"claim_index": 4, "source": "tool:infer_trade_skills:A-1"},
                          {"claim_index": 0, "source": "tool:get_permit:A-1"},
                          {"claim_index": 0, "source": "detail page"}])
    out = validate_report(TRANSCRIPT, r)
    assert out["integrity"] == "PASSED"
    assert [w["type"] for w in out["warnings"]] == ["citation_index", "citation_source", "citation_source"]


def test_get_permit_results_ground_permits_and_skills():
    transcript = [step("get_permit", {"permit_number": "D-4"},
                       {"permit": {"permit_number": "D-4"}, "skill_inference": {"skills": [SKILL]}})]
    assert transcript_permits(transcript) == {"D-4"}
    out = validate_report(transcript, report([{"permit_number": "D-4", "status": "ISSUED",
                                               "detail_present": False, "skills": [SKILL]}]))
    assert out["integrity"] == "PASSED"


def test_transcript_rebuilt_from_events():
    events = [{"event": "agent.start"},
              {"event": "tool.call", "tool": "search_permits", "params_json": {"status": ["ISSUED"]}, "ok": True,
               "latency_ms": 3, "result_json": {"result": TRANSCRIPT[1]["result"], "attempts": 1,
                                                "executed": True}}]
    rebuilt = transcript_from_events(events)
    assert transcript_permits(rebuilt) == {"A-1", "B-2"} and rebuilt[0]["executed"] is True

"""Stage 08 on Postgres: labeling workflow, harnesses H1-H7, judge guardrail, release gate, drift,
diagnosis, SLO, caching and /metrics. Acceptance criteria AC1-AC7 are named in the test names."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import JURISDICTION, TEST_DB_URL
from permit_trade_skills.eval import drift, gate, slo
from permit_trade_skills.eval import labels as L
from permit_trade_skills.eval.diagnose import diagnose
from permit_trade_skills.eval.harnesses import run_suite
from permit_trade_skills.eval.judge import run_judge
from permit_trade_skills.eval.results import save
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.store.eval import SqlEvalStore
from permit_trade_skills.wiring import eval_flagger

from eval_fakes import (MODEL, SETTINGS, T0, add_event, content, context, golden_trade_row, seed_grounded,
                        seed_inference, seed_permit, seed_snapshot, skill, skills_model, write_golden)
from query_fakes import nlu_payload


@pytest.fixture
def store(clean_db):
    return SqlEvalStore(TEST_DB_URL)


def review_row(engine, inference_id):
    with engine.connect() as c:
        return dict(c.execute(text("SELECT review_status, proposed_by, reviewer, proposed_decision "
                                   "FROM skill_inferences WHERE id = :i"), {"i": inference_id}).one()._mapping)


# ---- E1 labeling workflow ------------------------------------------------------------

def test_two_labelers_adjudication_export_and_supersede_ac1(clean_db, store, tmp_path):
    pid = seed_permit(clean_db, "1-CN")
    seed_snapshot(clean_db, pid, "1-CN", content("1-CN", scope="Kitchen remodel with new sink"))
    svc = L.LabelService(store, JURISDICTION)
    svc.annotate("trade_set", "1-CN", {"required_trades": ["Carpentry", "Plumbing"], "unrequired_trades": ["Roofing"],
                                       "difficulty": "easy"}, "alice")
    svc.annotate("trade_set", "1-CN", {"required_trades": ["Carpentry", "Electrical"], "optional_trades": ["Painting"],
                                       "difficulty": "medium"}, "bob")
    with pytest.raises(L.LabelError, match="must be someone else"):
        svc.adjudicate("1-CN", "alice", "conflict of interest")
    gold = svc.adjudicate("1-CN", "carol", "union per policy", drop={"Electrical": "no electrical scope stated"})
    p = gold["payload"]
    assert p["required_trades"] == ["Carpentry", "Plumbing"]          # union minus the noted drop
    assert p["disagreements"] == {"Plumbing": ["alice"], "Electrical": ["bob"]}
    assert p["labelers"] == ["alice", "bob"] and p["difficulty"] == "medium"
    out = svc.export("trade_set", tmp_path / "trade-set.jsonl")
    assert out["written"] == 1
    rows = L.load_golden(tmp_path / "trade-set.jsonl", "trade_set")
    assert rows[0]["status"] == L.ACTIVE and rows[0]["input"]["snapshot"]["content_text"].startswith("PERMIT: 1-CN")
    assert rows[0]["pinned_input_hash"] == gold["pinned_input_hash"] and rows[0]["adjudicated_by"] == "carol"

    # tampered file -> SUPERSEDED
    tampered = dict(rows[0])
    tampered["input"] = json.loads(json.dumps(rows[0]["input"]))
    tampered["input"]["snapshot"]["content_text"] += " plus a pool"
    L.write_jsonl(tmp_path / "t.jsonl", [tampered])
    assert L.load_golden(tmp_path / "t.jsonl", "trade_set")[0]["status"] == L.SUPERSEDED

    # a new snapshot (the input changed) supersedes every label made against the old one
    seed_snapshot(clean_db, pid, "1-CN", content("1-CN", scope="Kitchen remodel, scope revised"),
                  fetched_at=T0)
    live, stale = svc.current("trade_set", "1-CN", "labeler")
    assert live == [] and {r["labeled_by"] for r in stale} == {"alice", "bob"}
    assert store.current_labels(JURISDICTION, "trade_set", "1-CN", "labeler") == []
    assert svc.export("trade_set", tmp_path / "again.jsonl")["skipped"][0]["item_key"] == "1-CN"


def test_relabel_is_a_new_version_and_separation_of_duties(clean_db, store):
    ids = seed_grounded(clean_db, "2-CN")
    with clean_db.begin() as c:
        c.execute(text("UPDATE skill_inferences SET reviewer = 'dana' WHERE id = :i"), {"i": ids["inference_id"]})
    svc = L.LabelService(store, JURISDICTION)
    first = svc.annotate("trade_set", "2-CN", {"required_trades": ["Carpentry"]}, "erin")
    second = svc.annotate("trade_set", "2-CN", {"required_trades": ["Carpentry", "Plumbing"]}, "erin")
    assert (first["label_version"], second["label_version"]) == (1, 2)
    assert [r["id"] for r in store.current_labels(JURISDICTION, "trade_set", "2-CN")] == [second["id"]]
    with pytest.raises(L.LabelError, match="separation of duties"):
        svc.annotate("trade_set", "2-CN", {"required_trades": ["Carpentry"]}, "dana")


def test_nl_label_pins_gold_total_and_checklist_export(clean_db, store, tmp_path):
    for n in range(3):
        seed_permit(clean_db, f"{n}-CN", status="ISSUED")
    seed_permit(clean_db, "9-CN", status="PENDING")
    svc = L.LabelService(store, JURISDICTION)
    row = svc.annotate("nl_query", "q1", {"question": "Issued permits", "expected": {"status": ["ISSUED"]},
                                          "as_of": "2026-11-10"}, "frank")
    assert row["payload"]["gold_total"] == 3
    pid = store.permit(JURISDICTION, "0-CN")["id"]
    seed_snapshot(clean_db, pid, "0-CN", content("0-CN"))
    svc.annotate("extraction_checklist", "0-CN", {"must_appear": ["0-CN", "100 MAIN ST", "kitchen"]}, "frank")
    assert svc.export("nl_query", tmp_path / "nl.jsonl")["written"] == 1
    assert svc.export("extraction_checklist", tmp_path / "c.jsonl")["written"] == 1
    assert L.load_golden(tmp_path / "nl.jsonl", "nl_query")[0]["status"] == L.ACTIVE
    assert L.load_golden(tmp_path / "c.jsonl", "extraction_checklist", store=store)[0]["status"] == L.ACTIVE


# ---- H1 + AC3: the faithfulness re-check is independent of the writer ------------------------------

def test_h1_finds_a_planted_bad_row_and_flags_it_ac3(clean_db, store, tmp_path):
    for n in range(5):
        seed_grounded(clean_db, f"{n}-CN")
    pid = seed_permit(clean_db, "BAD-CN")
    sid = seed_snapshot(clean_db, pid, "BAD-CN", content("BAD-CN"))
    # the (fake) writer let through a non-verbatim quote
    bad = seed_inference(clean_db, pid, sid, [skill("BAD-CN", "Install a rooftop helipad with structural welding")])
    ctx = context(tmp_path, build_id="b-ac3", flag=eval_flagger(TEST_DB_URL, JURISDICTION))
    res = run_suite(ctx, "H1")
    assert res.metrics["faithfulness"] == round(5 / 6, 4)
    worst = res.worst[0]
    assert worst["key"] == f"BAD-CN#{bad}" and "evidence_not_verbatim" in worst["reason"]
    assert review_row(clean_db, bad) == {"review_status": "NEEDS_HUMAN", "proposed_by": "agent", "reviewer": None,
                                         "proposed_decision": "needs_human"}
    report = gate.evaluate(store, "b-ac3", settings=SETTINGS, skip=["H2", "H3", "H4", "H5", "H6", "H7"])
    assert report["status"] == gate.FAIL and report["failed"] == ["H1.faithfulness"]


# ---- H2 + AC6: recall/precision, cached re-runs ------------------------------------------------------------

def _golden_a(tmp_path, n_full=7, n_miss=3, miss_trade="Roofing"):
    rows = [golden_trade_row(f"{i}-CN", ["Carpentry", "Plumbing"], unrequired=["Roofing"]) for i in range(n_full)]
    rows += [golden_trade_row(f"M{i}-CN", ["Electrical"], unrequired=["Roofing"], difficulty="hard")
             for i in range(n_miss)]
    write_golden(tmp_path, "trade-set.jsonl", rows)
    full = {f"{i}-CN": [{"trade": "Carpentry", "skill": "Kitchen cabinet installation"},
                        {"trade": "Plumbing", "skill": "Sink supply and drain piping"}] for i in range(n_full)}
    miss = {f"M{i}-CN": [{"trade": miss_trade, "skill": f"{miss_trade} work on the kitchen"}] for i in range(n_miss)}
    return skills_model({**full, **miss})


def test_h2_scores_recall_precision_traps_and_reruns_from_cache_ac6(clean_db, tmp_path):
    provider = _golden_a(tmp_path)
    ctx = context(tmp_path, build_id="b-h2", skills_provider=lambda: provider)
    first = run_suite(ctx, "H2")
    assert first.llm_calls == 10 and len(provider.calls) == 10
    assert first.metrics["recall"] == 0.7
    assert first.metrics["trap_hallucination_rate"] == 0.3        # 3 of 10 traps claimed
    assert first.metrics["precision"] == 0.7 and first.metrics["raw_faithfulness"] == 1.0
    assert first.info["recall_by_difficulty"] == {"easy": 1.0, "medium": None, "hard": 0.0}
    again = run_suite(ctx, "H2")
    assert again.llm_calls == 0 and len(provider.calls) == 10 and again.cache_hits == 10
    assert again.metrics == first.metrics
    forced = run_suite(context(tmp_path, build_id="b-h2", skills_provider=lambda: provider, force=True), "H2")
    assert forced.llm_calls == 10


def test_h2_infers_completed_permits_without_saving(clean_db, tmp_path):
    write_golden(tmp_path, "trade-set.jsonl", [golden_trade_row("C-CN", ["Carpentry"], status="COMPLETED")])
    res = run_suite(context(tmp_path, skills_provider=lambda: skills_model({})), "H2")
    assert res.items[0]["error"] is None and res.metrics["recall"] == 1.0
    with clean_db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM skill_inferences")).scalar() == 0


# ---- AC2: the gate gates (good build passes, bad build fails on exactly the breached criteria) -------------

def _full_build(clean_db, tmp_path, build, *, bad):
    for n in range(99):
        seed_grounded(clean_db, f"G{n}-CN")
    pid = seed_permit(clean_db, "Z-CN")
    sid = seed_snapshot(clean_db, pid, "Z-CN", content("Z-CN"))
    evidence = "a quote that is not in the page" if bad else "Construct alterations to the kitchen"
    seed_inference(clean_db, pid, sid, [skill("Z-CN", evidence)])
    provider = _golden_a(tmp_path, n_full=7 if bad else 10, n_miss=3 if bad else 0, miss_trade="Painting")
    # H3 golden: two NL questions with gold totals; H4: a checklist; H6: an export of every permit
    svc = L.LabelService(SqlEvalStore(TEST_DB_URL), JURISDICTION)
    nl = []
    for qid, question, expected in (("q1", "Issued permits", {"status": ["ISSUED"]}),
                                    ("q2", "Pending permits", {"status": ["PENDING"]})):
        payload = L.validate_payload("nl_query", {"question": question, "expected": expected, "as_of": "2026-11-10"})
        payload["gold_total"] = svc.gold_total(payload)
        nl.append({"id": qid, "kind": "nl_query", "jurisdiction": JURISDICTION, **payload,
                   "pinned_input_hash": L.input_hash("nl_query", payload)})
    write_golden(tmp_path, "nl-query.jsonl", nl)
    nlu = FakeProvider(lambda s, u: nlu_payload(status=["PENDING"] if "Pending" in u else ["ISSUED"]))
    source = tmp_path / "export.json"
    with clean_db.connect() as c:
        numbers = [r[0] for r in c.execute(text("SELECT permit_number FROM permits"))]
    source.write_text(json.dumps([{"permitnum": n, "statuscurrent": "Issued"} for n in numbers]))
    with clean_db.begin() as c:
        c.execute(text("UPDATE jurisdictions SET field_map = :f, status_map = :s"),
                  {"f": json.dumps({"permit_num": "permitnum", "status_current": "statuscurrent"}),
                   "s": json.dumps({"issued": ["Issued"]})})
    ctx = context(tmp_path, build_id=build, skills_provider=lambda: provider, nlu_provider=lambda: nlu,
                  query_info=lambda: ({"slug": JURISDICTION, "name": "Test", "county": None, "state": "WA"}, {}),
                  source_file=source, flag=eval_flagger(TEST_DB_URL, JURISDICTION))
    for suite in ("H1", "H2", "H3", "H4", "H5", "H6", "H7"):
        run_suite(ctx, suite)


def test_gate_passes_a_good_build_ac2(clean_db, store, tmp_path):
    _full_build(clean_db, tmp_path, "good-fixture", bad=False)
    report = gate.evaluate(store, "good-fixture", settings=SETTINGS)
    assert report["status"] == gate.PASS, gate.format_report(report)
    assert gate.EXIT_CODES[report["status"]] == 0 and report["gate_id"]


def test_gate_fails_a_bad_build_on_the_breached_criteria_twice_ac2(clean_db, store, tmp_path):
    _full_build(clean_db, tmp_path, "bad-fixture", bad=True)
    for _ in range(2):   # idempotent and re-runnable
        report = gate.evaluate(store, "bad-fixture", settings=SETTINGS)
        assert report["status"] == gate.FAIL
        assert sorted(report["failed"]) == ["H1.faithfulness", "H2.recall"]
        by = {c["criterion"]: c for c in report["criteria"]}
        assert by["H1.faithfulness"]["measured"] == 0.99 and by["H2.recall"]["measured"] == 0.7
        assert [w["key"] for w in by["H1.faithfulness"]["worst"]][0].startswith("Z-CN#")
        assert len(by["H2.recall"]["worst"]) == 3 and all("missing Electrical" in w["reason"]
                                                          for w in by["H2.recall"]["worst"])
    with clean_db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM build_gates WHERE build_id = 'bad-fixture'")).scalar() == 2


def test_soft_breach_is_conditional_and_needs_signoff(clean_db, store, tmp_path):
    _full_build(clean_db, tmp_path, "soft", bad=False)
    report = gate.evaluate(store, "soft", settings={**SETTINGS, "eval": {
        **SETTINGS["eval"], "thresholds": {**SETTINGS["eval"]["thresholds"], "H2.recall": {"min": 1.01}}}})
    assert report["status"] == gate.CONDITIONAL and report["failed"] == ["H2.recall"]
    signed = gate.sign_off(store, "soft", "owner", "accepted for the pilot")
    assert signed["signed_off_by"] == "owner" and signed["promotable"]
    with pytest.raises(ValueError, match="already signed off"):
        gate.sign_off(store, "soft", "owner", "again")
    missing = gate.evaluate(store, "never-ran", settings=SETTINGS)
    assert missing["status"] == gate.FAIL and all(c["status"] == gate.MISSING for c in missing["criteria"])


# ---- H3: deterministic row-count deviation -----------------------------------------------------------

def test_h3_row_count_deviation_is_deterministic(clean_db, store, tmp_path):
    for n in range(20):
        seed_permit(clean_db, f"{n}-CN", status="ISSUED")
    rows = []
    for qid, gold in (("exact", 20), ("off", 22)):
        payload = {"question": f"Issued permits {qid}", "expected": {"status": ["ISSUED"]}, "as_of": "2026-11-10",
                   "gold_total": gold, "expected_unfulfilled": [], "expect_unfulfilled": False}
        rows.append({"id": qid, **payload, "pinned_input_hash": L.input_hash("nl_query", payload)})
    write_golden(tmp_path, "nl-query.jsonl", rows)
    nlu = FakeProvider(nlu_payload(status=["ISSUED"]))
    ctx = context(tmp_path, nlu_provider=lambda: nlu,
                  query_info=lambda: ({"slug": JURISDICTION, "name": "Test", "county": None, "state": "WA"}, {}))
    a, b = run_suite(ctx, "H3"), run_suite(ctx, "H3")
    for res in (a, b):
        by = {i["key"]: i for i in res.items}
        assert by["exact"]["row_deviation"] == 0.0 and by["exact"]["row_ok"]
        assert by["off"]["row_deviation"] == round(2 / 22, 4) and not by["off"]["row_ok"]
        assert by["off"]["full_match"]        # parsed right; the count check is separate
    assert b.llm_calls == 0 and a.metrics == b.metrics


# ---- H4, H5, H6, H7 ---------------------------------------------------------------------------------

def test_h4_checklists_live_sample_and_catalog_grounding(clean_db, store, tmp_path):
    pid = seed_permit(clean_db, "C1-CN")
    sid = seed_snapshot(clean_db, pid, "C1-CN", content("C1-CN"))
    snap = store.snapshot(sid)
    row = {"id": "C1-CN", "permit_number": "C1-CN", "must_appear": ["C1-CN", "100 MAIN ST", "kitchen", "garage"],
           "snapshot": {"id": sid, "content_hash": snap["content_hash"]},
           "pinned_input_hash": L.input_hash("extraction_checklist", {"permit_number": "C1-CN",
                                                                      "content_hash": snap["content_hash"]})}
    write_golden(tmp_path, "extraction.jsonl", [row])
    raw = {"permitnum": "S1-CN", "description": "Construct  a deck", "link": {"url": "https://x.test/S1"}}
    spid = seed_permit(clean_db, "S1-CN", raw_row=raw)
    seed_snapshot(clean_db, spid, "S1-CN", "PERMIT: S1-CN\nDESCRIPTION: Construct a deck\nSTATUS: Issued\n"
                  "ADDRESS: 100 MAIN ST", strategy="soda_row", degraded=True)
    res = run_suite(context(tmp_path), "H4")
    by = {i["key"]: i for i in res.items}
    assert by["C1-CN"]["missing"] == ["garage"] and not by["C1-CN"]["passed"]
    assert by["S1-CN"]["dataset"] == "live" and by["S1-CN"]["passed"]
    assert by["S1-CN#catalog"]["missing_fields"] == ["link.url"]      # a normalize bug signature
    assert res.metrics["catalog_grounding"] == 0.0 and res.info["fetch_strategy_mix"] == {
        "redirect_chase": 1, "soda_row": 1}


def test_h5_revalidates_stored_agent_runs(clean_db, tmp_path):
    pid = seed_permit(clean_db, "A1-CN")
    search = {"ok": True, "data": {"rows": [{"permit_number": "A1-CN"}]}}
    for run, finding in (("agent-ok", "A1-CN"), ("agent-bad", "NOPE-CN")):
        add_event(clean_db, run, "agent.start")
        add_event(clean_db, run, "tool.call", tool="search_permits", params_json={},
                  result_json={"result": search, "attempts": 1, "executed": True}, ok=True)
        add_event(clean_db, run, "agent.finish", permit_id=pid, result_json={
            "goal": "g", "integrity": "PASSED", "unfulfilled": [],
            "findings": [{"permit_number": finding, "skills": []}]})
    res = run_suite(context(tmp_path), "H5")
    by = {i["key"]: i for i in res.items}
    assert by["agent-ok"]["passed"] and not by["agent-bad"]["passed"]
    assert by["agent-bad"]["integrity_mismatch"] and res.metrics["fabrications"] == 1.0
    assert res.counts["integrity_mismatch"] == 1


def test_h6_loss_and_status_drift(clean_db, store, tmp_path):
    for n in range(4):
        seed_permit(clean_db, f"{n}-CN")
    with clean_db.begin() as c:
        c.execute(text("UPDATE jurisdictions SET field_map = :f, status_map = :s"),
                  {"f": json.dumps({"permit_num": "permitnum", "status_current": "statuscurrent"}),
                   "s": json.dumps({"issued": ["Issued"]})})
    source = tmp_path / "export.json"
    source.write_text(json.dumps([{"permitnum": f"{n}-CN", "statuscurrent": "Issued"} for n in range(5)]))
    res = run_suite(context(tmp_path, source_file=source), "H6")
    assert res.metrics["loss"] == 0.2 and res.worst[0]["key"] == "4-CN"
    assert res.metrics["unknown_share"] == 0.0 and res.counts == {"source_rows": 5, "store_rows": 4, "missing": 1}


def test_h7_counts_illegal_transitions_and_self_approvals(clean_db, tmp_path):
    ids = seed_grounded(clean_db, "S-CN", review_status="APPROVED", reviewer=None, state="SKILLS_APPROVED")
    seed_permit(clean_db, "T-CN", state="SKILLS_APPROVED")
    add_event(clean_db, "pipe-1", "transition", from_state="SYNCED", to_state="SKILLS_APPROVED",
              error_code="ILLEGAL_TRANSITION", ok=False)
    res = run_suite(context(tmp_path), "H7")
    assert res.metrics == {"violations": 3.0, "illegal_transitions": 1.0, "self_approvals": 2.0}
    assert any(i["key"] == f"S-CN#{ids['inference_id']}" for i in res.items)


# ---- E3 + AC4: the judge flags, never rejects ----------------------------------------------------------------

def test_judge_flags_a_gate_exploit_as_needs_human_ac4(clean_db, tmp_path):
    pid = seed_permit(clean_db, "EX-CN")
    sid = seed_snapshot(clean_db, pid, "EX-CN", content("EX-CN"))
    # the quote is verbatim (passes the grounding gate) but does not support the skill
    exploit = seed_inference(clean_db, pid, sid, [skill("EX-CN", "Fire Alarm: No",
                                                        name="Fire alarm system installation",
                                                        trade="Fire Protection")])
    judge = FakeProvider(lambda s, u: {"skills": [{
        "index": 0, "capability_phrase": {"score": 2, "reason": "a capability"},
        "evidence_supports": {"score": 0, "reason": "the quote says there is no fire alarm"},
        "evidence_best": {"score": 0, "reason": "no supporting text"}}],
        "assumptions_honest": {"score": 2, "reason": "none needed"}}, model="fake-judge")
    ctx = context(tmp_path, build_id="b-judge", flag=eval_flagger(TEST_DB_URL, JURISDICTION))
    res = run_judge(ctx, lambda: judge, judge_model="fake/fake-judge", generator_model=MODEL, rate=1.0)
    save(ctx.store, res)
    item = res.items[0]
    assert item["flags"][0]["gate_passed"] is True and item["auto_rejected"] is False
    assert item["flagged"] and item["review_status"] == "NEEDS_HUMAN"
    row = review_row(clean_db, exploit)
    assert row["review_status"] == "NEEDS_HUMAN" and row["proposed_by"] == "agent" and row["reviewer"] is None
    with clean_db.connect() as c:
        assert c.execute(text("SELECT processing_state FROM permits WHERE id = :p"), {"p": pid}).scalar() == \
            "SKILLS_INFERRED"                                   # not rejected: the human decides
    assert run_suite(ctx, "H1").metrics["faithfulness"] == 1.0    # the string gate still passes it
    cached = run_judge(ctx, lambda: judge, judge_model="fake/fake-judge", generator_model=MODEL, rate=1.0)
    assert cached.llm_calls == 0 and cached.cache_hits == 1


# ---- E5 + AC5: drift detection and diagnosis of a "portal changed" cohort ---------------------------------------

def test_portal_changed_cohort_raises_a_drift_alert_and_diagnosis_points_at_the_page_ac5(clean_db, store, tmp_path):
    for n in range(20):    # baseline: 10% degraded, 3-6 days ago
        pid = seed_permit(clean_db, f"OLD{n}-CN")
        seed_snapshot(clean_db, pid, f"OLD{n}-CN", content(f"OLD{n}-CN"), degraded=n < 2,
                      strategy="soda_row" if n < 2 else "redirect_chase", fetched_at=T0 - timedelta(days=3 + n % 3))
    for n in range(20):    # last 24h: 60% degraded, and the page lost its address line
        pid = seed_permit(clean_db, f"NEW{n}-CN")
        changed = n < 12
        text_ = content(f"NEW{n}-CN") if not changed else f"PERMIT: NEW{n}-CN\nSTATUS: Issued\nDESCRIPTION: x"
        seed_snapshot(clean_db, pid, f"NEW{n}-CN", text_, degraded=changed,
                      strategy="soda_row" if changed else "redirect_chase", fetched_at=T0 - timedelta(hours=2 + n % 5))
    result = drift.check(store, JURISDICTION, settings=SETTINGS, now=T0)
    alert = next(a for a in result["alerts"] if a["detector"] == "degraded_share")
    assert alert["suspected_cause"] == "detail_extraction" and alert["severity"] == "critical"
    assert "10%" in alert["message"] and "60%" in alert["message"]
    assert store.alerts(detector="degraded_share")[0]["id"] == alert["id"]
    assert drift.check(store, JURISDICTION, settings=SETTINGS, now=T0)["alerts"] == []   # deduplicated

    res = run_suite(context(tmp_path, settings={**SETTINGS, "eval": {**SETTINGS["eval"], "h4_live_sample": 40}}),
                    "H4")
    assert res.metrics["live_pass"] == round(28 / 40, 4)
    d = diagnose(store, JURISDICTION, "H4", timedelta(days=7), now=T0 + timedelta(minutes=5))
    assert d["likely_root_cause"] == "source_page_changed" and d["cohort"]["page_changed_signature"]
    assert all(i["permit_number"].startswith("NEW") for i in d["items"]) and len(d["items"]) == 5
    assert "ADDRESS" not in d["items"][0]["excerpt"] and "detail strategy" in d["next_step"]


def test_confidence_shift_detector_names_a_model_change(clean_db, store):
    for n in range(30):
        ids = seed_grounded(clean_db, f"K{n}-CN", confidence="high" if n < 25 else "low",
                            created_at=T0 - timedelta(days=3))
    for n in range(30):
        seed_grounded(clean_db, f"L{n}-CN", confidence="low" if n < 25 else "high", model="fake/new-model",
                      created_at=T0 - timedelta(hours=3))
    result = drift.check(store, JURISDICTION, settings=SETTINGS, now=T0)
    shift = next(d for d in result["detectors"] if d["detector"] == "confidence_shift")
    assert shift["breach"] and shift["suspected_cause"] == "model_change"
    assert ids


# ---- AC7: SLO measurability ----------------------------------------------------------------------

def test_slo_on_a_seeded_50_permit_window_ac7(clean_db, store):
    """Hand computation: 50 ISSUED permits first synced 25-47h before now, issued 24h before their sync.
    40 are inferred 1-10h after sync (within 24h), 5 after 30h (late), 5 never -> 40/50 = 0.8.
    Issued -> ready hours: 25..34 four times each + 54 five times (45 values); P95 position 0.95 * 44 = 41.8
    -> the 42nd and 43rd smallest are both 54 -> 54.0."""
    now = T0
    for i in range(50):
        created = now - timedelta(hours=25 + (i % 23))
        pid = seed_permit(clean_db, f"SLO{i}-CN", created_at=created, issued_at=created - timedelta(hours=24))
        if i < 40:
            seed_inference(clean_db, pid, None, [], created_at=created + timedelta(hours=1 + i % 10))
        elif i < 45:
            seed_inference(clean_db, pid, None, [], created_at=created + timedelta(hours=30))
    rep = slo.report(store, JURISDICTION, timedelta(hours=48), settings=SETTINGS, now=now)
    assert rep["cohort"] == 50 and rep["measured"] == 50 and rep["pending"] == 0
    assert rep["inference_within_pct"] == 0.8 and not rep["inference_ok"]
    assert rep["p95_ready_hours"] == 54.0 and rep["p95_ok"]


# ---- /metrics ----------------------------------------------------------------------------------

def test_metrics_endpoint_exposes_prometheus_gauges(clean_db, store, tmp_path):
    from fastapi.testclient import TestClient

    from permit_trade_skills.query.service import QueryService
    from permit_trade_skills.server import create_app

    seed_grounded(clean_db, "M-CN", confidence="high")
    run_suite(context(tmp_path, build_id="m"), "H1")
    app = create_app(QueryService(TEST_DB_URL), settings={"api": {"rate_limit_per_minute": 0}}, eval_store=store)
    body = TestClient(app).get("/metrics").text
    assert 'pts_pipeline_state_count{state="SKILLS_INFERRED"} 1' in body
    assert 'pts_skill_overall_confidence_count{level="high"} 1' in body
    assert 'pts_eval_metric{suite="H1",metric="faithfulness"} 1' in body
    assert "# TYPE pts_detail_fetch_ok gauge" in body


def test_label_cli_single_labeler_adjudication_with_labeler_alias(clean_db, store):
    from typer.testing import CliRunner

    from permit_trade_skills import app

    pid = seed_permit(clean_db, "CLI-CN")
    seed_snapshot(clean_db, pid, "CLI-CN", content("CLI-CN"))
    runner = CliRunner()
    base = ["--db-url", TEST_DB_URL, "--jurisdiction", JURISDICTION]
    r = runner.invoke(app, ["label", "annotate", "-p", "CLI-CN", "--labeler", "draft-x", "--required", "Carpentry",
                            *base])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["label", "judge", "-p", "CLI-CN", "--labeler", "ton-adjudicator",
                            "--note", "Single labeler phase", "--single-labeler", *base])
    assert r.exit_code == 0, r.output
    gold = store.current_labels(JURISDICTION, "trade_set", "CLI-CN", "adjudicator")
    assert [g["labeled_by"] for g in gold] == ["ton-adjudicator"]
    assert gold[0]["payload"]["single_labeler"] and gold[0]["payload"]["labelers"] == ["draft-x"]

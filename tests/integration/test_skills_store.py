"""Stage 06 against Postgres: SqlSkillStore, the 0005 audit columns, idempotency, state transitions, show."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from permit_trade_skills.skills.schemas import InferenceFailed
from permit_trade_skills.wiring import skill_service
from skills_fakes import SETTINGS, fixture_provider, load

J = "seattle-kingcounty-wa"


def seed(engine, name: str, *, state: str = "DETAIL_READY", with_snapshot: bool = True) -> int:
    fx = load(name)
    p, d = fx["permit"], fx["detail"]
    with engine.begin() as conn:
        pid = conn.execute(text(
            "INSERT INTO permits (jurisdiction_id, jurisdiction_slug, permit_number, status, status_raw, "
            "permit_class, permit_class_mapped, permit_type_mapped, permit_type_desc, valuation, issued_at, "
            "address, description, detail_url, processing_state, raw_row, raw_hash, run_id, created_at, updated_at) VALUES "
            "(:j, :j, :num, :status, 'Issued', :cls, :clsm, :typ, :typd, :val, :iss, :addr, :desc, :url, :state, "
            "'{}', 'h', 'test', now(), now()) RETURNING id"),
            {"j": J, "num": p["permit_number"], "status": p["status"], "cls": p["permit_class"],
             "clsm": p["permit_class_mapped"], "typ": p["permit_type_mapped"], "typd": p["permit_type_desc"],
             "val": p["valuation"], "iss": p["issued_at"], "addr": p["address"], "desc": p["description"],
             "url": p["detail_url"], "state": state}).scalar_one()
        if with_snapshot and d:
            sid = conn.execute(text(
                "INSERT INTO detail_snapshots (permit_id, final_url, fetch_strategy, content_text, content_hash) "
                "VALUES (:pid, :url, :strat, :text, :hash) RETURNING id"),
                {"pid": pid, "url": d["final_url"], "strat": d["fetch_strategy"], "text": d["content_text"],
                 "hash": d["content_hash"]}).scalar_one()
            assert sid
    return pid


def service(db_url, *names):
    return skill_service(db_url, provider=fixture_provider(*names), settings=SETTINGS)


def test_infer_persists_audit_row_and_is_idempotent(clean_db, db_url):
    pid = seed(clean_db, "basement")
    svc = service(db_url, "basement")
    first = svc.infer("7083363-CN", J)
    second = svc.infer("7083363-CN", J)
    assert second.cached and second.inference_id == first.inference_id
    assert len(svc.provider.calls) == 1
    with clean_db.connect() as conn:
        rows = conn.execute(text("SELECT * FROM skill_inferences")).mappings().all()
        state = conn.execute(text("SELECT processing_state FROM permits WHERE id = :i"), {"i": pid}).scalar_one()
    assert len(rows) == 1 and state == "SKILLS_INFERRED"
    row = rows[0]
    assert row["review_status"] == "PENDING" and row["overall_confidence"] == "medium"
    assert row["prompt_version"] == "skill-infer-v1" and row["model"] == "fake/fake-skills"
    for col in ("prompt_hash", "completion_hash", "context_hash", "input_cache_key", "substrate_hash"):
        assert row[col]
    assert row["degraded"] is False and row["substrate_text"] is None and row["dropped_skills"] == []
    assert row["signal_check"]["first_pass_rate"] == 1.0
    assert second.to_contract()["skills"] == first.skills


def test_unique_cache_key_under_a_race(clean_db, db_url):
    pid = seed(clean_db, "basement")
    from permit_trade_skills.store.skills import SqlSkillStore

    store = SqlSkillStore(db_url)
    row = {"permit_id": pid, "prompt_version": "v", "model": "m", "skills": [], "assumptions": [],
           "input_cache_key": "k1"}
    first = store.save_inference(row)
    again = store.save_inference(row)
    assert first[1] is True and again == (first[0], False)


def test_overall_confidence_check_constraint(clean_db, db_url):
    pid = seed(clean_db, "basement")
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with clean_db.begin() as conn:
            conn.execute(text("INSERT INTO skill_inferences (permit_id, prompt_version, model, skills, "
                              "overall_confidence) VALUES (:p, 'v', 'm', '[]', 'certain')"), {"p": pid})


def test_wrong_permit_marks_failed_with_state_error(clean_db, db_url):
    fx = load("wrong_permit")
    pid = seed(clean_db, "wrong_permit")
    svc = service(db_url, "wrong_permit")
    with pytest.raises(InferenceFailed):
        svc.infer(fx["permit"]["permit_number"], J)
    with clean_db.connect() as conn:
        state, err = conn.execute(text("SELECT processing_state, state_error FROM permits WHERE id = :i"),
                                  {"i": pid}).one()
        n = conn.execute(text("SELECT count(*) FROM skill_inferences")).scalar_one()
    assert state == "SKILLS_FAILED" and err["error"] == "GROUNDING_FAILED" and n == 0


def test_approved_permit_state_is_not_touched(clean_db, db_url):
    pid = seed(clean_db, "basement", state="SKILLS_APPROVED")
    service(db_url, "basement").infer("7083363-CN", J)
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT processing_state FROM permits WHERE id = :i"),
                            {"i": pid}).scalar_one() == "SKILLS_APPROVED"


def test_degraded_row_keeps_its_substrate_and_show_verifies_it(clean_db, db_url):
    fx = load("basement")
    seed(clean_db, "basement", state="SYNCED", with_snapshot=False)
    url = fx["permit"]["detail_url"]
    response = {"skills": [{"skill": "Interior partition framing for basement alterations", "trade": "Carpentry",
                            "evidence": fx["permit"]["description"], "evidence_source": url,
                            "confidence": "medium"}],
                "assumptions": [], "overall_confidence": "medium", "licensing_notes": []}
    from skills_fakes import SequenceProvider

    svc = skill_service(db_url, provider=SequenceProvider([response]), settings=SETTINGS)
    inf = svc.infer("7083363-CN", J, allow_degraded=True)
    assert inf.degraded and inf.overall_confidence == "low"
    shown = svc.show("7083363-CN", J)
    assert shown["evidence_source"] == url and all(t["grounded"] for t in shown["grounding"])
    assert shown["processing_state"] == "SKILLS_INFERRED"


def test_cli_show_prints_a_grounding_table(clean_db, db_url, monkeypatch):
    from typer.testing import CliRunner

    from permit_trade_skills import app

    seed(clean_db, "basement")
    service(db_url, "basement").infer("7083363-CN", J)
    result = CliRunner().invoke(app, ["skills", "show", "--permit", "7083363-CN", "--db-url", db_url])
    assert result.exit_code == 0, result.output
    assert result.output.count("✓") == 4          # 3 skills + the summary line
    assert "Grounding: 3/3" in result.output
    as_json = CliRunner().invoke(app, ["skills", "show", "--permit", "7083363-CN", "--json", "--db-url", db_url])
    assert json.loads(as_json.output)["contract"]["permit_key"] == f"{J}:7083363-CN"

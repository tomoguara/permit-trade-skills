"""Shared helpers for the Stage 08 tests: seed permits, snapshots, inferences and events on the test
database, build golden rows, and fake LLMs (skills, NLU, judge)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text

from conftest import JURISDICTION, TEST_DB_URL
from permit_trade_skills.eval import labels as L
from permit_trade_skills.eval.harnesses import EvalContext
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.store.eval import SqlEvalStore

T0 = datetime(2026, 11, 10, 12, 0)
MODEL = "fake/fake-skills"
SETTINGS: Dict[str, Any] = {
    "skills": {"temperature": 0.1, "max_tokens": 4000, "max_context_chars": 8000, "allowed_statuses": ["ISSUED"]},
    "eval": {
        "trade_set_statuses": ["ISSUED", "COMPLETED"], "h4_live_sample": 15,
        "judge": {"temperature": 0.0, "max_tokens": 2000, "sample_rate": 0.30},
        "thresholds": {
            "H1.faithfulness": {"min": 1.0, "hard": True}, "H2.recall": {"min": 0.80},
            "H2.trap_hallucination_rate": {"max": 0.10}, "H3.full_match": {"min": 0.90},
            "H4.must_appear": {"min": 0.90}, "H4.catalog_grounding": {"min": 1.0, "hard": True},
            "H5.fabrications": {"max": 0, "hard": True}, "H6.loss": {"max": 0.0, "hard": True},
            "H7.violations": {"max": 0, "hard": True}},
        "drift": {"window_hours": 24, "baseline_days": 7, "min_samples": 10, "ks_alpha": 0.01,
                  "unknown_status_jump_pts": 2.0, "degraded_jump_pts": 20.0, "h2_recall_min": 0.8},
        "slo": {"inference_within_hours": 24, "inference_target": 0.95, "p95_ready_hours": 72},
    },
}


def final_url(number: str) -> str:
    return f"https://portal.example.test/cap/{number}"


def detail_url(number: str) -> str:
    return f"https://portal.example.test/link?altId={number}"


def content(number: str, address: str = "100 MAIN ST", scope: str = "Construct alterations to the kitchen",
            status: str = "Issued", extra: Sequence[str] = ()) -> str:
    return "\n".join([f"PERMIT: {number}", f"STATUS: {status}", f"ADDRESS: {address}", f"DESCRIPTION: {scope}",
                      "Fire Alarm: No", *extra])


def seed_permit(engine, number: str, *, status: str = "ISSUED", address: str = "100 MAIN ST",
                description: str = "Construct alterations to the kitchen", created_at: Optional[datetime] = None,
                issued_at: Optional[datetime] = None, raw_row: Optional[Dict[str, Any]] = None,
                state: str = "SYNCED", updated_at: Optional[datetime] = None) -> int:
    row = raw_row or {"permitnum": number, "statuscurrent": status.title(), "description": description}
    created = created_at or T0 - timedelta(days=30)
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO permits (jurisdiction_id, jurisdiction_slug, permit_number, status, status_raw, "
            "permit_class_mapped, permit_type_desc, description, address, issued_at, detail_url, raw_row, raw_hash, "
            "run_id, created_at, updated_at, processing_state) VALUES (:j, :j, :n, :st, :sr, 'Residential', "
            "'Addition/Alteration', :d, :a, :i, :u, :row, :h, 'test', :c, :up, :state) RETURNING id"),
            {"j": JURISDICTION, "n": number, "st": status, "sr": status.title(), "d": description, "a": address,
             "i": issued_at or created - timedelta(days=1), "u": detail_url(number), "row": json.dumps(row),
             "h": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), "c": created,
             "up": updated_at or created, "state": state}).scalar_one()


def seed_snapshot(engine, permit_id: int, number: str, text_: str, *, strategy: str = "redirect_chase",
                  degraded: bool = False, fetched_at: Optional[datetime] = None,
                  source_hash: Optional[str] = None) -> int:
    with engine.begin() as conn:
        return conn.execute(text(
            "INSERT INTO detail_snapshots (permit_id, final_url, fetch_strategy, content_text, content_hash, "
            "fetched_at, degraded, source_hash) VALUES (:p, :u, :s, :t, :h, :f, :d, :sh) RETURNING id"),
            {"p": permit_id, "u": final_url(number), "s": strategy, "t": text_,
             "h": hashlib.sha256(text_.encode()).hexdigest(), "f": fetched_at or T0 - timedelta(days=2),
             "d": degraded, "sh": source_hash}).scalar_one()


def skill(number: str, evidence: str, name: str = "Kitchen cabinet and counter installation",
          trade: str = "Carpentry", confidence: str = "medium") -> Dict[str, Any]:
    return {"skill": name, "trade": trade, "evidence": evidence, "evidence_source": final_url(number),
            "confidence": confidence}


def seed_inference(engine, permit_id: int, snapshot_id: Optional[int], skills: List[Dict[str, Any]], *,
                   model: str = MODEL, confidence: str = "medium", created_at: Optional[datetime] = None,
                   review_status: str = "PENDING", reviewer: Optional[str] = None,
                   proposed_by: Optional[str] = None, state: str = "SKILLS_INFERRED") -> int:
    with engine.begin() as conn:
        inf_id = conn.execute(text(
            "INSERT INTO skill_inferences (permit_id, prompt_version, model, skills, input_snapshot_id, "
            "review_status, reviewer, proposed_by, overall_confidence, created_at, input_cache_key) "
            "VALUES (:p, 'skill-infer-v1', :m, :s, :snap, :rs, :rv, :pb, :c, :t, :k) RETURNING id"),
            {"p": permit_id, "m": model, "s": json.dumps(skills), "snap": snapshot_id, "rs": review_status,
             "rv": reviewer, "pb": proposed_by, "c": confidence, "t": created_at or T0 - timedelta(days=1),
             "k": f"key-{permit_id}-{snapshot_id}"}).scalar_one()
        conn.execute(text("UPDATE permits SET processing_state = :s WHERE id = :p"), {"s": state, "p": permit_id})
    return inf_id


def seed_grounded(engine, number: str, **kw) -> Dict[str, int]:
    """A permit + snapshot + an accepted, grounded inference."""
    pid = seed_permit(engine, number)
    sid = seed_snapshot(engine, pid, number, content(number))
    iid = seed_inference(engine, pid, sid, [skill(number, "Construct alterations to the kitchen")], **kw)
    return {"permit_id": pid, "snapshot_id": sid, "inference_id": iid}


def add_event(engine, run_id: str, event: str, **kw) -> None:
    cols = {"run_id": run_id, "event": event, "created_at": kw.pop("created_at", T0), **kw}
    for k in ("params_json", "result_json"):
        if k in cols:
            cols[k] = json.dumps(cols[k])
    names = ", ".join(cols)
    values = ", ".join(f":{k}" for k in cols)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO orchestration_events ({names}) VALUES ({values})"), cols)


# ---- golden rows ---------------------------------------------------------------

def golden_trade_row(number: str, required: Sequence[str], *, status: str = "ISSUED", optional: Sequence[str] = (),
                     unrequired: Sequence[str] = (), difficulty: str = "easy", scope: Optional[str] = None,
                     degraded: bool = False) -> Dict[str, Any]:
    text_ = content(number, scope=scope or f"Construct alterations to the kitchen of {number}")
    permit = {"permit_number": number, "status": status, "permit_class": "Single Family/Duplex",
              "permit_class_mapped": "Residential", "permit_type_mapped": "Building",
              "permit_type_desc": "Addition/Alteration", "valuation": 50000.0, "issued_at": "2026-11-01T00:00:00",
              "address": "100 MAIN ST", "city": "SEATTLE", "state": "WA", "zip_code": "98101",
              "description": scope or f"Construct alterations to the kitchen of {number}", "detail_url": detail_url(number)}
    snap = {"id": 1, "final_url": final_url(number), "fetch_strategy": "soda_row" if degraded else "redirect_chase",
            "content_hash": hashlib.sha256(text_.encode()).hexdigest(), "degraded": degraded, "content_text": text_,
            "fetched_at": "2026-11-09T00:00:00"}
    inp = L.trade_input(permit, snap)
    return {"id": number, "kind": "trade_set", "jurisdiction": JURISDICTION, "permit_number": number, "input": inp,
            "label": {"required_trades": list(required), "optional_trades": list(optional),
                      "unrequired_trades": list(unrequired), "defensible_skills": {}, "difficulty": difficulty,
                      "notes": ""},
            "labelers": ["a", "b"], "adjudicated_by": "c", "label_version": 1, "labeled_at": "2026-11-10T00:00:00",
            "pinned_input_hash": L.input_hash("trade_set", inp)}


def write_golden(directory: Path, name: str, rows: List[Dict[str, Any]]) -> Path:
    path = directory / name
    L.write_jsonl(path, rows)
    return path


def skills_model(answers: Dict[str, List[Dict[str, str]]]) -> FakeProvider:
    """A skills.infer fake: per permit number, a list of {trade, skill} answered with the scope quote."""

    def respond(system, user):
        number = re.search(r"PERMIT # (\S+)", user).group(1)
        url = re.search(r"SOURCE URL: (\S+)", user).group(1)
        quote = re.search(r"DESCRIPTION: (.+)", user).group(1).strip()
        items = answers.get(number, [{"trade": "Carpentry", "skill": "Interior framing and finish carpentry"}])
        return {"skills": [{"skill": a["skill"], "trade": a["trade"], "evidence": quote, "evidence_source": url,
                            "confidence": "medium"} for a in items],
                "assumptions": [], "overall_confidence": "medium", "licensing_notes": []}

    return FakeProvider(respond, model="fake-skills")


def context(golden_dir: Path, **kw) -> EvalContext:
    kw.setdefault("settings", SETTINGS)
    return EvalContext(store=SqlEvalStore(TEST_DB_URL), jurisdiction=JURISDICTION, golden_dir=golden_dir, **kw)

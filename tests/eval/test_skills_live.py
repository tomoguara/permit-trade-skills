"""Stage 06 fixture eval: the 5 positive + 3 negative skill fixtures through `infer_skills`.

Default (CI): the fixture LLM (canned responses keyed by context_hash); no
network. Live: `PTS_SKILLS_LIVE=1 uv run pytest tests/eval/test_skills_live.py -s`
uses the provider configured for role `skills.infer` (config/settings.yaml:
Ollama qwen3.8:latest or OpenAI gpt-4.1), prints a per-fixture report and
writes the evidence JSON to evals/evidence/seattle/skills/<date>-live.json.
The nightly CI job `skills-live` runs live.

Live assertions (a real model is not scripted):
- every positive fixture is accepted, and every accepted skill is grounded
  (re-verified here against the fixture text);
- first-pass grounding rate over the positive fixtures >= LIVE_FIRST_PASS;
- no fabrication: no skill for a fixture's `absent` trades;
- recall of the fixture's `present` trades >= LIVE_RECALL (reported per fixture);
- injection: no Welding skill, no quote of the injected text;
- wrong permit: GROUNDING_FAILED; admin-only: NO_SKILLS, or an accepted low-confidence answer.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from permit_trade_skills.skills import grounding as G
from permit_trade_skills.skills.infer import infer_skills
from permit_trade_skills.skills.schemas import GROUNDING_FAILED, NO_SKILLS, InferenceFailed
from skills_fakes import NEGATIVE, POSITIVE, SETTINGS, detail_document, fixture_provider, load, permit_input

LIVE = os.environ.get("PTS_SKILLS_LIVE") == "1"
EVIDENCE = Path(__file__).resolve().parents[2] / "evals" / "evidence" / "seattle" / "skills"
LIVE_FIRST_PASS = 0.9
LIVE_RECALL = 0.8


def _provider(name):
    if LIVE:
        from permit_trade_skills.llm import get_provider

        return get_provider("skills.infer")
    return fixture_provider(name)


def _covers(skills, words):
    return any(w in f"{s['skill']} {s['trade']}".lower() for s in skills for w in words)


def _run(name):
    fx = load(name)
    try:
        inf = infer_skills(permit_input(fx), detail_document(fx), provider=_provider(name), settings=SETTINGS)
        return fx, inf, None
    except InferenceFailed as e:
        return fx, None, e


def test_skill_fixtures():
    report, checked, accepted, recall_hits, recall_total = [], 0, 0, 0, 0
    problems = []
    for name in POSITIVE + NEGATIVE:
        fx, inf, err = _run(name)
        exp = fx["expected"]
        entry = {"fixture": name, "permit_number": fx["permit"]["permit_number"],
                 "expected_outcome": exp["outcome"]}
        if err is not None:
            entry.update(outcome=err.code, error=err.state_error())
        else:
            entry.update(outcome="accepted", inference=inf.to_dict())
            text, url = fx["detail"]["content_text"], fx["detail"]["final_url"]
            regrounded = G.verify_row(inf.skills, text, url)
            if any(r["reasons"] for r in regrounded):
                problems.append(f"{name}: accepted skill fails re-verification")
        if name in POSITIVE:
            if inf is None:
                problems.append(f"{name}: expected accepted, got {entry['outcome']}")
                report.append(entry)
                continue
            checked += len(inf.skills) + len(inf.dropped_skills)
            accepted += len(inf.skills)
            hits = [words for words in exp["present"] if _covers(inf.skills, words)]
            recall_hits += len(hits)
            recall_total += len(exp["present"])
            entry["present_found"] = f"{len(hits)}/{len(exp['present'])}"
            fabricated = [w for w in exp["absent"] if _covers(inf.skills, [w])]
            entry["fabricated"] = fabricated
            if fabricated:
                problems.append(f"{name}: skills for absent trades {fabricated}")
            if not LIVE:
                assert inf.overall_confidence in exp["overall_in"], name
                assert len(inf.skills) == exp["skills"] and len(inf.dropped_skills) == exp["dropped"], name
        elif name == "injection":
            if inf is None or _covers(inf.skills, ["weld"]) or any(
                    G.normalize(exp["injected"]) in G.normalize(s["evidence"]) for s in inf.skills):
                problems.append("injection: obeyed or failed")
        elif name == "wrong_permit":
            if entry["outcome"] != GROUNDING_FAILED:
                problems.append(f"wrong_permit: expected GROUNDING_FAILED, got {entry['outcome']}")
        elif name == "admin_only":
            ok = entry["outcome"] == NO_SKILLS or (inf is not None and inf.overall_confidence == "low")
            if not ok:
                problems.append(f"admin_only: expected NO_SKILLS or low confidence, got {entry['outcome']}")
        report.append(entry)

    first_pass = accepted / checked if checked else 0.0
    recall = recall_hits / recall_total if recall_total else 0.0
    summary = {"live": LIVE, "first_pass_grounding": round(first_pass, 4), "present_trade_recall": round(recall, 4),
               "problems": problems}
    print(f"\nskills fixtures ({'live' if LIVE else 'fixture LLM'}): first-pass grounding {first_pass:.1%}, "
          f"present-trade recall {recall:.1%}")
    for e in report:
        inf = e.get("inference") or {}
        print(f"  {e['fixture']:<19} {e['outcome']:<17} skills={len(inf.get('skills', []))} "
              f"dropped={len(inf.get('dropped_skills', []))} overall={inf.get('overall_confidence', '-')} "
              f"present={e.get('present_found', '-')} fabricated={e.get('fabricated', '-')}")
    for p in problems:
        print(f"  PROBLEM: {p}")

    if LIVE:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        model = next((e["inference"]["model"] for e in report if e.get("inference")), None)
        out = EVIDENCE / f"{date.today().isoformat()}-live.json"
        out.write_text(json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "test": "tests/eval/test_skills_live.py", "model": model, **summary,
                                   "fixtures": report}, indent=2, default=str, ensure_ascii=False) + "\n")
        print(f"  evidence: {out}")

    assert not problems, problems
    assert first_pass >= (LIVE_FIRST_PASS if LIVE else 0.98)
    assert recall >= (LIVE_RECALL if LIVE else 1.0)

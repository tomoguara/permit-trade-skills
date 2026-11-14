"""Golden NL set (Stage 04 AC1): >= 95% of the golden questions must parse to exactly the
expected PermitQuery filters.

Default (CI): a fake NLU that answers with the expected parse checks the
harness, the golden file and the parse -> PermitQuery path; no LLM is called.
Live: `PTS_NLU_LIVE=1 uv run pytest tests/eval -s` parses with the provider
configured in config/settings.yaml (`llm.use_local_ollama_model` picks Ollama or
OpenAI) and prints a per-question report. The nightly CI job runs live.

Expected values may use relative-date tokens resolved against today:
@today-<N>d, @year-start, @month-start, @last-month-start.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.query.nlu import NLUParser, interpret, match_jurisdictions
from permit_trade_skills.query.params import FILTER_PARAMS, parse_params
from permit_trade_skills.skills.schemas import TRADES
from query_fakes import SEATTLE, nlu_payload

GOLDEN = Path(__file__).resolve().parents[2] / "evals" / "golden" / "seattle" / "nl-questions.jsonl"
THRESHOLD = 0.95
LIVE = os.environ.get("PTS_NLU_LIVE") == "1"
# Seattle's distinct permit_class / permit_class_mapped / permit_type_mapped values,
# as QueryService.known_values() supplies them.
KNOWN = {
    "permit_class": ["Commercial", "Industrial", "Institutional", "Multifamily", "N/A", "Non-Residential",
                     "Residential", "Single Family/Duplex", "Vacant Land"],
    "permit_type": ["Building", "Demolition", "ECA and Shoreline Exemption/Street Improvement Exception Request",
                    "Grading", "Roof"],
    "trade": list(TRADES),
}


def resolve_token(value: Any, today: date) -> Any:
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    if m := re.fullmatch(r"@today-(\d+)d", value):
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    month_start = today.replace(day=1)
    return {
        "@year-start": today.replace(month=1, day=1),
        "@month-start": month_start,
        "@last-month-start": (month_start - timedelta(days=1)).replace(day=1),
    }[value].isoformat()


def load_golden(today: date) -> List[Dict[str, Any]]:
    cases = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    for case in cases:
        case["expected"] = {k: resolve_token(v, today) for k, v in case["expected"].items()}
    return cases


def normalize(query) -> Dict[str, Any]:
    """Comparable form of a PermitQuery's filters (+ sort when not the default)."""
    out: Dict[str, Any] = {}
    for name in FILTER_PARAMS:
        value = getattr(query, name)
        if value is None:
            continue
        if isinstance(value, datetime):
            value = value.date().isoformat()
        elif isinstance(value, (list, tuple)):
            value = sorted(str(v).lower() for v in value)
        elif isinstance(value, str):
            value = value.lower()
        out[name] = value
    if query.sort.value != "issued_at_desc":
        out["sort"] = query.sort.value
    return out


def expected_query(case: Dict[str, Any]):
    return parse_params(SEATTLE["slug"], case["expected"])[0]


def run_case(parser: NLUParser, case: Dict[str, Any]) -> Dict[str, Any]:
    parsed = parser.parse(case["question"], None, KNOWN)
    executed, problems, _ = interpret(parsed, SEATTLE)   # the same step POST /query runs
    named = parsed.jurisdiction_in_question
    return {
        "id": case["id"],
        "match": normalize(executed) == normalize(expected_query(case)),
        "got": normalize(executed),
        "want": normalize(expected_query(case)),
        "unfulfilled": list(parsed.unrecognized_entities) + problems,
        "assumptions": parsed.assumptions,
        "jurisdiction_ok": not named or match_jurisdictions(named, [SEATTLE]) == [SEATTLE["slug"]],
    }


def make_parser(today: date) -> NLUParser:
    if LIVE:
        from permit_trade_skills.llm import get_provider

        return NLUParser(get_provider("nlu.parser"), cache=None, today=lambda: today)

    by_question = {c["question"]: c for c in load_golden(today)}

    def respond(system: str, user: str):
        question = user.rsplit("Question: ", 1)[1]
        case = by_question[question]
        pq = dict(case["expected"])
        for key in ("permit_class", "permit_type"):
            if isinstance(pq.get(key), list):
                pq[key] = pq[key][0]
        return nlu_payload(unrecognized=["near the Space Needle"] if case.get("expect_unfulfilled") else [],
                           jurisdiction="Seattle" if "Seattle" in question else None, **pq)

    return NLUParser(FakeProvider(respond), cache=None, today=lambda: today)


def test_golden_file_is_well_formed():
    cases = load_golden(date.today())
    assert len(cases) >= 20 and len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        expected_query(case)   # every expected parse is a valid PermitQuery


def test_relative_tokens():
    today = date(2026, 3, 15)
    assert resolve_token("@today-90d", today) == "2025-12-15"
    assert resolve_token("@last-month-start", today) == "2026-02-01"
    assert resolve_token("@month-start", today) == "2026-03-01"
    assert resolve_token("@year-start", today) == "2026-01-01"


def test_nlu_golden_set():
    today = date.today()
    parser = make_parser(today)
    results = [run_case(parser, case) for case in load_golden(today)]
    rate = sum(r["match"] for r in results) / len(results)

    print(f"\nNLU golden set ({'live: ' + parser.model if LIVE else 'fake NLU'}): "
          f"{sum(r['match'] for r in results)}/{len(results)} exact ({rate:.0%})")
    for r in results:
        if not r["match"]:
            print(f"  MISS {r['id']}: got {r['got']} want {r['want']} "
                  f"unfulfilled={r['unfulfilled']} assumptions={r['assumptions']}")

    assert all(r["jurisdiction_ok"] for r in results), "a question about Seattle resolved elsewhere"
    geo = next(r for r in results if r["id"] == "nl-08")
    assert geo["unfulfilled"], "the Space Needle constraint must be reported, not dropped"
    assert rate >= THRESHOLD, f"exact-parse rate {rate:.0%} < {THRESHOLD:.0%}"

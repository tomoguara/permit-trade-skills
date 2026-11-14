#!/usr/bin/env python
"""Phase-1 query demo (Stage 04 definition of done): three sample questions, traceable answers.

    uv run python script/demo_query.py [--jurisdiction seattle-kingcounty-wa] [--db-url URL]

1. "Which residential permits were issued in the last 90 days?" (natural language -> NLU)
2. Permit counts by status since 2026-01-01 (permit_status_counts tool)
3. A keyword search: "Show solar panel permits" (natural language -> NLU)

For each answer it prints the executed query, the template echo, the trace id
and the permit numbers. It then re-runs the executed query through the
structured search tool and checks that the permit numbers are identical.
The NLU uses the provider selected in config/settings.yaml.
"""

from __future__ import annotations

import argparse
import json
import uuid

from permit_trade_skills.query.service import QueryService, current_trace_id
from permit_trade_skills.query.tools import call_tool

SLUG = "seattle-kingcounty-wa"
STRUCTURED_KEYS = ("status", "statuses_exclude", "permit_class", "permit_type", "desc_keyword", "city",
                   "zip", "applied_after", "applied_before", "issued_after", "issued_before",
                   "min_cost", "max_cost", "sort", "page_size")


def section(title: str) -> None:
    print("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)


def nl_question(service: QueryService, question: str, slug: str) -> None:
    trace = uuid.uuid4().hex
    current_trace_id.set(trace)
    r = service.ask(question, slug, page_size=10)
    print(f"Q: {question}")
    print(f"echo:   {r['echo']}")
    print(f"query:  {json.dumps(r['query'])}")
    print(f"nlu:    {r['nlu']['model']} ({'cached' if r['nlu']['cached'] else 'fresh'} parse, "
          f"{r['nlu']['prompt_version']})")
    for key in ("assumptions", "ambiguous_terms", "unfulfilled_filters"):
        if r[key]:
            print(f"{key}: {r[key]}")
    print(f"rows:   {r['count']} shown, ~{r['total_estimate']:,} matching (planner estimate); trace_id {trace}")
    for row in r["rows"]:
        cost = f"${row['valuation']:,.0f}" if row["valuation"] is not None else "n/a"
        print(f"  {row['permit_number']:<13} {row['status']:<10} {(row['issued_at'] or '')[:10]:<10} "
              f"{cost:>11}  {(row['description'] or '')[:60]}")
        print(f"  {'':<13} {row['detail_url'] or ''}")

    # Grounding check: the same query through the structured tool returns the same permits.
    args = {k: v for k, v in r["query"].items() if k in STRUCTURED_KEYS}
    check = call_tool("search_permits", {"jurisdiction": slug, **args}, service)
    same = [x["permit_number"] for x in check["data"]["rows"]] == [x["permit_number"] for x in r["rows"]]
    print(f"verify: structured search_permits returns the same {len(r['rows'])} permit numbers: "
          f"{'OK' if same else 'MISMATCH'}")


def status_counts(service: QueryService, slug: str) -> None:
    result = call_tool("permit_status_counts", {"jurisdiction": slug, "since": "2026-01-01"}, service)
    data = result["data"]
    print("Q: How many permits are in each status, for applications since 2026-01-01?")
    print(f"window: {data['date_field']} >= {data['since']}")
    for status, n in data["counts"].items():
        print(f"  {status:<14} {n:>7,}")
    print(f"  {'total':<14} {data['total']:>7,}")
    issued = call_tool("search_permits", {"jurisdiction": slug, "status": ["ISSUED"],
                                          "applied_after": "2026-01-01", "page_size": 200}, service)
    print(f"verify: search_permits ISSUED since 2026-01-01 first page has "
          f"{issued['data']['count']} rows (counts are exact GROUP BY; search total is an estimate "
          f"~{issued['data']['total_estimate']:,})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jurisdiction", default=SLUG)
    ap.add_argument("--db-url", default=None)
    args = ap.parse_args()
    service = QueryService(args.db_url)

    section("1. Issued residential permits, last 90 days (natural language)")
    nl_question(service, "Which residential permits were issued in the last 90 days?", args.jurisdiction)
    section("2. Counts by status since 2026-01-01 (permit_status_counts tool)")
    status_counts(service, args.jurisdiction)
    section("3. Keyword search (natural language)")
    nl_question(service, "Show solar panel permits", args.jurisdiction)


if __name__ == "__main__":
    main()

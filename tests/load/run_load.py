#!/usr/bin/env python
"""Stage 04 AC7 load test: GET /api/v1/permits and POST /api/v1/query at 200k rows.

    uv run python tests/load/run_load.py                  # 200k rows, 400 requests per endpoint
    uv run python tests/load/run_load.py --rows 200000 --requests 1000 --concurrency 8 --json out.json

What it does:
1. Recreates the disposable database `permit_trade_skills_load_test` (name must
   end in _test), migrates it, registers the Seattle slug and COPY-loads the
   deterministic 200k-row fixture (script/gen_fixture.py, anchor 2026-11-01).
2. Serves the real app (create_app) in-process with uvicorn. The NLU is a
   FakeProvider that sleeps 300 ms per parse (the "mocked 300ms NLU"), the
   rate limiter is off, and every /query question is unique, so each request
   misses the NLU cache and pays the 300 ms.
3. Fires the requests from an async httpx client at the given concurrency and
   reports p50/p95/p99/max per endpoint against the targets:
   /permits p95 < 150 ms, /query p95 < 1200 ms.

Exit code 1 if a target is missed. Results for the record go in docs/api.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "script")]

DEFAULT_URL = os.environ.get(
    "PTS_LOAD_DB_URL", "postgresql://postgres:postgres@localhost:5432/permit_trade_skills_load_test")
SLUG = "seattle-kingcounty-wa"
ANCHOR = datetime(2026, 11, 1)
TARGETS_MS = {"permits": 150, "query": 1200}


def prepare_db(url: str, rows: int) -> None:
    from sqlalchemy import create_engine

    from conftest import copy_rows, insert_jurisdiction, make_permit_rows, recreate_database
    from permit_trade_skills.store import schema

    t0 = time.monotonic()
    recreate_database(url)
    schema.upgrade(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        insert_jurisdiction(conn, SLUG)
    engine.dispose()
    copy_rows(url, make_permit_rows(rows, seed=42, anchor=ANCHOR))
    print(f"loaded {rows:,} rows into {url.rsplit('/', 1)[-1]} in {time.monotonic() - t0:.1f}s")


def start_server(url: str, port: int):
    import uvicorn

    from permit_trade_skills.llm.fake import FakeProvider
    from permit_trade_skills.query.nlu import NLUParser
    from permit_trade_skills.query.service import DbNluCache, QueryService
    from permit_trade_skills.server import create_app
    from permit_trade_skills.settings import DEFAULTS, _merge
    from query_fakes import nlu_payload

    service = QueryService(url)
    parse = nlu_payload(status=["ISSUED"], permit_class="Residential",
                        issued_after=(ANCHOR - timedelta(days=90)).date().isoformat(), min_cost=50000)
    service._nlu = NLUParser(FakeProvider(parse, latency_s=0.3), DbNluCache(service._sessions))
    app = create_app(service=service, settings=_merge(DEFAULTS, {"api": {"rate_limit_per_minute": 0}}))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread


PERMIT_QUERIES = [
    {"status": "ISSUED", "issued_after": (ANCHOR - timedelta(days=90)).date().isoformat()},   # hot path
    {"status": "PENDING", "applied_after": (ANCHOR - timedelta(days=30)).date().isoformat()},
    {"status": "ISSUED,COMPLETED", "permit_class": "Residential", "min_cost": 50000},
    {"desc_keyword": "roof", "status": "COMPLETED", "page_size": 50},
    {"zip": "98122", "sort": "cost_desc"},
]


async def fire(base: str, kind: str, n: int, concurrency: int) -> list:
    import uuid

    import httpx

    nonce = uuid.uuid4().hex[:8]   # unique questions per run: every /query misses the NLU cache

    latencies: list = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        async def one(i: int) -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                if kind == "permits":
                    r = await client.get("/api/v1/permits",
                                         params={"jurisdiction": SLUG, **PERMIT_QUERIES[i % len(PERMIT_QUERIES)]})
                else:
                    r = await client.post("/api/v1/query", json={
                        "question": f"issued residential permits over $50k in the last 90 days (load {nonce}-{i})",
                        "jurisdiction": SLUG})
                latencies.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    errors += 1

        await asyncio.gather(*(one(i) for i in range(n)))
    if errors:
        raise SystemExit(f"{kind}: {errors} non-200 responses")
    return latencies


def summarize(kind: str, latencies: list) -> dict:
    q = statistics.quantiles(latencies, n=100)
    return {"endpoint": kind, "requests": len(latencies), "p50_ms": round(q[49], 1),
            "p95_ms": round(q[94], 1), "p99_ms": round(q[98], 1), "max_ms": round(max(latencies), 1),
            "target_p95_ms": TARGETS_MS[kind], "passed": q[94] < TARGETS_MS[kind]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=DEFAULT_URL)
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--requests", type=int, default=400, help="requests per endpoint")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--skip-load", action="store_true", help="reuse the already-loaded database")
    ap.add_argument("--json", type=Path, help="also write the results as JSON")
    args = ap.parse_args()

    if not args.skip_load:
        prepare_db(args.db_url, args.rows)
    server, thread = start_server(args.db_url, args.port)
    base = f"http://127.0.0.1:{args.port}"
    try:
        asyncio.run(fire(base, "permits", 20, 4))   # warm-up (connection pool, plans)
        results = [summarize(kind, asyncio.run(fire(base, kind, args.requests, args.concurrency)))
                   for kind in ("permits", "query")]
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    meta = {"rows": args.rows, "concurrency": args.concurrency, "nlu_mock_ms": 300,
            "run_at": datetime.now().isoformat(timespec="seconds")}
    print(json.dumps(meta))
    print(f"{'endpoint':<9} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  target p95")
    for r in results:
        print(f"{r['endpoint']:<9} {r['requests']:>5} {r['p50_ms']:>7.1f}ms {r['p95_ms']:>7.1f}ms "
              f"{r['p99_ms']:>7.1f}ms {r['max_ms']:>7.1f}ms  < {r['target_p95_ms']}ms "
              f"{'PASS' if r['passed'] else 'FAIL'}")
    if args.json:
        args.json.write_text(json.dumps({**meta, "results": results}, indent=2) + "\n")
    sys.exit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()

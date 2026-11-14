#!/usr/bin/env python
"""Deterministic synthetic permit fixture generator + loader.

    python script/gen_fixture.py --rows 200000 --out tests/fixtures/permits_200k.jsonl.gz

Each line is one canonical `permits` row (column -> value). Same --seed and
--rows always produce byte-identical output. Issue dates span the 10 years up
to --anchor-date, so "issued in the last 90 days" is a small, realistic slice.
raw_hash is computed exactly as ingestion does, so `pts db doctor` passes on
loaded fixtures.

`load_fixture()` bulk-loads a fixture with COPY (used by the tests).
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from permit_trade_skills.ingest.normalizers import compute_raw_hash

STATUSES = [("COMPLETED", 70), ("EXPIRED", 7), ("PENDING", 9), ("CANCELED", 5),
            ("ISSUED", 5), ("IN_INSPECTION", 2), ("UNKNOWN", 2)]
CLASSES = [("Single Family/Duplex", "Residential"), ("Multifamily", "Residential"),
           ("Commercial", "Non-Residential"), ("Institutional", "Non-Residential"),
           ("Industrial", "Non-Residential"), ("N/A", "N/A")]
TYPES = ["Building", "Roof", "Demolition", "Mechanical", "Electrical", "Grading"]
WORK = ["roof replacement", "deck addition", "kitchen remodel", "new single family residence",
        "basement finish", "seismic retrofit", "solar panel installation", "garage conversion",
        "window replacement", "tenant improvement", "foundation repair", "ADU construction"]
ZIPS = [f"981{n:02d}" for n in range(1, 100, 3)]

COLUMNS = [
    "jurisdiction_id", "jurisdiction_slug", "permit_number", "permit_class", "permit_class_mapped",
    "permit_type_mapped", "permit_type_desc", "status_raw", "status", "issue_date", "issued_at",
    "applied_at", "expires_at", "completed_at", "description", "address", "city", "state",
    "zip_code", "latitude", "longitude", "valuation", "housing_units", "raw_row", "raw_hash",
    "row_id_source", "detail_url", "run_id", "created_at", "updated_at", "synced_at",
    "processing_state",
]


def _weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def generate(rows: int, seed: int = 42, jurisdiction: str = "seattle-kingcounty-wa",
             anchor_date: Optional[datetime] = None) -> Iterator[dict]:
    rng = random.Random(seed)
    anchor = anchor_date or datetime(2026, 11, 1)
    span_days = 3650
    for i in range(rows):
        status = _weighted(rng, STATUSES)
        cls, cls_mapped = rng.choice(CLASSES)
        work = rng.choice(WORK)
        applied = anchor - timedelta(days=rng.uniform(0, span_days), seconds=rng.randrange(86400))
        issued = applied + timedelta(days=rng.uniform(1, 120)) if status != "PENDING" else None
        if issued and issued > anchor:
            issued = anchor - timedelta(hours=rng.randrange(1, 48))
        cost = None if rng.random() < 0.15 else round(rng.lognormvariate(10, 1.2), 2)
        number = f"{7000000 + i}-{rng.choice(['CN', 'RR', 'EX', 'DM'])}"
        raw = {"permitnum": number, "statuscurrent": status.title(), "description": f"{work} #{i}",
               "permitclass": cls, "estprojectcost": None if cost is None else f"{cost:.2f}"}
        ts = anchor.isoformat()
        yield {
            "jurisdiction_id": jurisdiction, "jurisdiction_slug": jurisdiction,
            "permit_number": number, "permit_class": cls, "permit_class_mapped": cls_mapped,
            "permit_type_mapped": rng.choice(TYPES), "permit_type_desc": "Addition/Alteration",
            "status_raw": status.title(), "status": status,
            "issue_date": issued.isoformat() if issued else None,
            "issued_at": issued.isoformat() if issued else None,
            "applied_at": applied.isoformat(),
            "expires_at": (issued + timedelta(days=540)).isoformat() if issued else None,
            "completed_at": (issued + timedelta(days=rng.uniform(30, 400))).isoformat()
            if issued and status == "COMPLETED" else None,
            "description": f"{work} #{i}",
            "address": f"{rng.randrange(100, 9999)} {rng.choice(['PINE', 'OAK', 'ALOHA', 'MAIN'])} ST",
            "city": "SEATTLE", "state": "WA", "zip_code": rng.choice(ZIPS),
            "latitude": round(47.49 + rng.random() * 0.24, 6),
            "longitude": round(-122.43 + rng.random() * 0.21, 6),
            "valuation": cost, "housing_units": rng.choice([0, 0, 0, 1, 2]),
            "raw_row": raw, "raw_hash": compute_raw_hash(raw),
            "row_id_source": f"row-{i}", "detail_url": None, "run_id": "fixture",
            "created_at": ts, "updated_at": ts, "synced_at": ts, "processing_state": "SYNCED",
        }


def write_fixture(path: Path, rows: int, seed: int = 42) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the gzip header (and so the file) byte-identical across runs
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for row in generate(rows, seed):
            gz.write((json.dumps(row) + "\n").encode())
    return path


def iter_fixture(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt") as f:
        for line in f:
            yield json.loads(line)


def load_rows(conn, rows) -> int:
    """COPY canonical rows into permits using an open psycopg connection. Returns count."""
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY permits ({', '.join(COLUMNS)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row([json.dumps(row[c]) if c == "raw_row" else row[c] for c in COLUMNS])
                n += 1
    return n


def load_fixture(db_url: str, path: Path) -> int:
    import psycopg

    with psycopg.connect(db_url) as conn:
        n = load_rows(conn, iter_fixture(path))
        conn.execute("ANALYZE permits")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures/permits_200k.jsonl.gz"))
    ap.add_argument("--load", metavar="DB_URL", help="Also COPY the fixture into this database")
    args = ap.parse_args()
    write_fixture(args.out, args.rows, args.seed)
    print(f"Wrote {args.rows} rows to {args.out}")
    if args.load:
        print(f"Loaded {load_fixture(args.load, args.out)} rows")


if __name__ == "__main__":
    main()

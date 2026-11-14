# Permit Ingestion Guide

How to pull building-permit records into the canonical `permits` table with
`pts sync` (Stage 02), and what a sync does.

> **Current state (2026-11-09).** The Seattle open-data API is down, so Seattle
> is synced from the downloaded export with `--local-file`. The Socrata path
> (incremental window, schema-drift check, rate limiting) is tested against a
> fake server only.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [What a sync does](#what-a-sync-does)
- [CLI Commands](#cli-commands)
- [Configuration](#configuration)
- [Runbook](#runbook)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Search with embeddings](#search-with-embeddings)

---

## Overview

![Ingestion Component Architecture](diagrams/ingestion-component.png)

![External Systems Deployment](diagrams/ingestion-deployment.png)

A sync:

1. **Pulls** rows from the jurisdiction's Socrata API, or from a local JSON export (`--local-file`).
2. **Normalizes** each row to the canonical permit shape using the jurisdiction's `field_map` and `status_map`, keeping the raw row verbatim.
3. **Embeds** each new or changed description (`all-MiniLM-L6-v2`, 384 dimensions) for semantic search.
4. **Upserts** into `permits` keyed on `(jurisdiction_id, permit_number)`, in one transaction, writing only rows that changed.
5. **Records** status changes in `status_history` and a run report in `sync_metadata`.

---

## Prerequisites

```bash
./script/pgvector.sh --start      # PostgreSQL 18 + pgvector in Docker
uv run pts db migrate             # schema (sync does not create tables)
uv run pts seeds seattle          # register seattle-kingcounty-wa
```

The jurisdiction must be registered before syncing. For Seattle use the seed;
discovery and registration are described in [how-to-guide.md](how-to-guide.md).

The database URL comes from `--db-url`, else `PERMIT_TRADE_SKILLS_DB_URL` (shell
or `.env`), else `postgresql://postgres:postgres@localhost:5432/permit_trade_skills`.
The first sync downloads the embedding model from Hugging Face; set `HF_TOKEN`
in `.env` for authenticated downloads.

---

## Quick Start

```bash
# Sync the Seattle export (~193k permits)
uv run pts sync sync -j seattle-kingcounty-wa --local-file data/wa-seattle-building-permits.json

# A small trial run
uv run pts sync sync -j seattle-kingcounty-wa --local-file data/wa-seattle-building-permits.json --limit 1000

# What would change, exactly; write nothing
uv run pts sync sync -j seattle-kingcounty-wa --local-file data/wa-seattle-building-permits.json --limit 1000 --dry-run

# Recent runs
uv run pts sync sync-report -j seattle-kingcounty-wa --last 5
```

The first sync of the export takes a while, mostly computing embeddings (~9
minutes on CPU); progress is printed to stderr. Re-running it is safe and fast:
an identical file is skipped (`identical_pull`), and otherwise only new or
changed descriptions are embedded and only changed permits are written.

---

## What a sync does

### Sources

| Source | How | Notes |
|---|---|---|
| Local JSON export | `--local-file <path>` | A JSON array of Socrata rows. `--limit N` takes the first N rows. Recorded as mode `full` with no cursor. |
| Socrata API | no `--local-file` | Reads `<base_url>/resource/<dataset_id>.json` in pages of 1,000 rows ordered by the permit-number column (ascending), until the last page or `--limit`. Seattle's API is down at the moment. |

### Incremental vs full (Socrata only)

An incremental sync (the default) pulls

```
(<applied column> >= today − window)  OR  (<timestamp_field> > cursor)
```

- `timestamp_field` is the jurisdiction's `detail_strategy.timestamp_field`
  (Seattle: `issueddate`); the cursor is its latest value pulled by the last
  run (`cursor_after`, a date).
- The applied column is `field_map["applied_at"]` (Seattle: `applieddate`);
  the window is 90 days, or `detail_strategy.refresh_window_days` if larger.
  It re-pulls recently applied permits so their status changes are picked up
  even when they were issued before the cursor. Permits applied before the
  window that change status are only refreshed by `--full`.
- `--since YYYY-MM-DD` replaces the window start for one run (e.g. after an
  outage).
- No cursor yet (first Socrata sync, or the latest run was a local file):
  everything is pulled. No `timestamp_field`: everything, every time (with a
  `no_timestamp_field` warning).
- `--full` pulls everything and needs `--yes` (see below).

### Identical pulls

Each run stores a fingerprint of what it pulled (each row's permit number and
`raw_hash`, plus the config version and the normalizer version). If a run pulls
exactly what the last successful run did, it does nothing and says so
(`identical_pull`, all rows `unchanged`). `--full` always reprocesses.

### Schema drift (Socrata only)

Before pulling, sync reads the dataset's `columns.json`:

- If the column for the permit number, status, issue date,
  `timestamp_field`, or (when the window is used) applied date is gone, the run
  stops with `SchemaViolation`: nothing is written, the failed run is saved
  (`schema_violation` warning listing the missing columns) and the command
  exits 1. Fix the map (see [Runbook](#runbook)) and sync again.
- Any other mapped column that is gone gives a `missing_column` warning; that
  field stays NULL.

### Politeness

Socrata requests are spaced per host: `ingest.rate_limit_per_minute` in
`config/settings.yaml` (default 60), unless the jurisdiction sets a rate for
the host in `detail_strategy.detail.rate_limits_per_minute` (Seattle: 20/min
for `cos-data.seattle.gov`, so a full pull of ~194 pages takes ~10 minutes).
Every request sends `ingest.user_agent`; add a contact (URL or email) there.
`PTS_INGEST_RATE_LIMIT_PER_MINUTE` and `PTS_INGEST_USER_AGENT` override them.

### Failures

The client retries HTTP 429/5xx and network errors three times (1, 2, 4 s, or
the server's `Retry-After`, up to 60 s). If the source still fails, or answers
with another 4xx, the run stops before writing anything: no permits change, the
run is saved to `sync_metadata` with `error` set and the cursor unchanged, a
`source_unavailable` warning says whether retrying later can help
(`retryable`), and the command exits 1.

### Normalization

- Each canonical field is read from the source column named in `field_map`
  (e.g. `permit_num → permitnum`).
- `status_raw` keeps the source value; `status` is the canonical status whose
  `status_map` list contains it (case-insensitive), else `UNKNOWN`.
- Dates are parsed from ISO strings (`2025-11-03T00:00:00.000`, `2025-11-03`,
  `...Z`) and stored as UTC; cost (`valuation`, commas allowed; `0.0` is kept),
  integers (`"1.0"` → 1) and coordinates become numbers. A value that can't be
  parsed becomes NULL with an `unparseable_date` / `unparseable_number`
  warning on the permit.
- Text fields are trimmed and runs of whitespace collapsed to one space;
  `status_raw` is kept exactly as received and the permit number is only
  trimmed.
- `source_updated_at` is the row's Socrata `:updated_at`, else the issue date,
  else NULL with a `missing_source_updated_at` warning.
- Coordinates outside -90..90 / -180..180 are set to NULL with an
  `invalid_coordinates` warning on the permit.
- `detail_url` is the row's link (the `detail_url` column in `field_map`,
  default `link`); otherwise the jurisdiction's `detail_strategy.detail_url_rule`
  with `{{column}}` placeholders filled from the row (Seattle:
  `...LinkToRecord.aspx?altId={{permitnum}}`); otherwise NULL with a
  `missing_detail_url` warning.
- `state` falls back to the jurisdiction's state when the row has none; `city`
  has no fallback.
- `raw_row` keeps the whole source row (including Socrata's `:id`, `:version`,
  `:created_at`, `:updated_at`); `raw_hash` is a SHA-256 of it without the
  `:` keys.
- New permits get `processing_state = SYNCED`. A re-sync never changes
  `processing_state`, so detail extraction and skill inference progress is kept.
- Rows without a permit number can't be stored. They are counted in
  `Dropped` and listed in a `missing_permit_number` warning (position in the
  pull and Socrata `:id`, first 100). Every run satisfies
  `pull_count = inserted + updated + unchanged + dropped`.

### Upsert

- A dry run does the same upsert and rolls it back, so its counts and status
  transitions are exactly what the real run would do.
- New permit → inserted.
- Existing permit → compared with the stored row. An empty incoming value keeps
  the stored one; `0` and `0.0` are real values and overwrite. If nothing
  changed, the row is not written (its `synced_at` stays the same). If
  something changed, the changed columns plus `updated_at`, `synced_at` and
  `run_id` are written. A stored embedding is kept unless the description
  changed.
- When a permit's canonical status changes, a `status_history` row is added
  (`from_status`, `to_status`, `raw_status`, `run_id`), and the report counts it
  under `status_transitions` (e.g. `{"ISSUED->COMPLETED": 12}`).
- The whole run is one transaction: if anything fails, nothing is written.

### Sync report (`sync_metadata`)

One row per sync that isn't a dry run, including failed ones: `run_id`, `jurisdiction_id`,
`jurisdiction_slug`, `config_version`, `mode` (`full`/`incremental`),
`pull_count`, `inserted`, `updated`, `unchanged`, `dropped`, `warnings`,
`status_transitions`, `duration_ms`, `cursor_before`, `cursor_after`,
`max_applied_at` (latest applied date pulled), `max_synced_at` (latest source
`:updated_at` pulled), `fingerprint`, `error`, `started_at`, `completed_at`.

---

## CLI Commands

### `pts sync sync`

```bash
pts sync sync --jurisdiction <slug> [OPTIONS]
```

| Option | Description | Default |
|---|---|---|
| `--jurisdiction, -j` | Jurisdiction id, or a unique prefix of one (required) | |
| `--db-url` | Database URL | see Prerequisites |
| `--full, -f` | Pull everything (no cursor/window) and reprocess even an identical pull. Socrata syncs need `--yes` | `false` |
| `--yes, -y` | Confirm a `--full` Socrata sync | `false` |
| `--since` | Socrata: pull rows applied on/after `YYYY-MM-DD` (replaces the window start); not with `--full`/`--local-file` | |
| `--dry-run, -n` | Pull, normalize and count exactly what would change; write nothing and save no report | `false` |
| `--limit, -l` | Maximum rows to pull | all |
| `--local-file` | Read rows from a JSON export instead of Socrata | |

Example output:

```
Loading data/wa-seattle-building-permits.json ...
Loaded 193,249 rows
Normalizing 1,000 rows ...
Computing embeddings for 998 descriptions ...
Upserting 1,000 rows (single transaction) ...
Committing ...
Upserted rows: inserted=1000, updated=0, unchanged=0
Sync complete for seattle-kingcounty-wa
Run ID: 4b0d6f2e-...
Mode: full
Config version: 3
Duration: 41250ms
Pull count: 1000
Inserted: 1000
Updated: 0
Unchanged: 0
Dropped: 0
```

Status transitions and cursors follow when there are any; a dry run says `Sync
dry run complete (nothing written)` and `Would be Inserted/Updated`. The command
exits with code 1 if the jurisdiction is unknown, the file is missing, `--full`
lacks `--yes`, or the run failed (`Sync failed for ...`, then the error).
Warnings (`unknown_status`, `missing_permit_number`, `missing_detail_url`,
`identical_pull`, `missing_column`, `schema_violation`, `source_unavailable`, ...)
are printed after the counts.

`--full` on a Socrata sync without `--yes` prints an estimate and stops:

```
--full re-pulls the whole dataset: about 193,249 rows in 194 requests (rate-limited), plus embeddings for new or changed descriptions.
Re-run with --yes to proceed.
```

`--jurisdiction` accepts a unique prefix of an active jurisdiction's id
(`-j seattle` → `Resolved jurisdiction 'seattle' to 'seattle-kingcounty-wa'`);
the full id is what gets stored. An ambiguous prefix is an error.

### `pts sync sync-report`

```bash
pts sync sync-report --jurisdiction <slug> [--last 10] [--output reports.json]
```

Prints each recent run, newest first: id, mode, config version, completion
time, duration, pull/insert/update/unchanged/dropped counts, cursors, status
transitions, warnings and error. With `--output`, writes them as a JSON list,
adding `started_at`, `max_applied_at`, `max_synced_at` and `fingerprint`.

---

## Configuration

Sync reads the jurisdiction's row in `jurisdictions`:

```sql
SELECT id, config_version, field_map, status_map, detail_strategy
FROM jurisdictions WHERE id = 'seattle-kingcounty-wa';
```

### Status map

Canonical status (lower-case) → list of raw source values:

```json
{
  "pending":   ["Initiated", "Pending", "Ready for Intake", "Reviews In Process", "..."],
  "issued":    ["Issued", "Phase Issued", "Active"],
  "completed": ["Completed", "Closed", "Inspections Completed", "Approved to Occupy"],
  "canceled":  ["Canceled", "Withdrawn", "Denied"],
  "expired":   ["Expired"]
}
```

Keys must be canonical statuses: `pending`, `issued`, `in_inspection`,
`completed`, `expired`, `canceled`. Raw values not listed become `UNKNOWN` and
are reported as `unknown_status` warnings.

Other keys sync reads from `detail_strategy`: `timestamp_field`,
`refresh_window_days`, `detail_url_rule`, `machine_readable`, and
`detail.rate_limits_per_minute`.

---

## Runbook

### Daily sync

```bash
uv run pts sync sync -j seattle-kingcounty-wa                 # incremental: window + cursor
uv run pts sync sync-report -j seattle-kingcounty-wa --last 1
```

After an outage longer than the window, add `--since <last good day>`.

### Full backfill

```bash
uv run pts sync sync -j seattle-kingcounty-wa --full          # prints the estimate, exits 1
uv run pts sync sync -j seattle-kingcounty-wa --full --yes
```

Use it after changing the field or status map, to refresh old permits whose
status changed outside the window, or to reprocess an identical pull.

### A source column was renamed or removed

The next Socrata sync stops with `SchemaViolation` (or warns `missing_column`
for a non-essential column).

1. Look at the dataset's columns (`<base_url>/api/views/<dataset_id>/columns.json`).
2. Fix the map: for Seattle edit `SEATTLE_FIELD_MAP` in
   `src/permit_trade_skills/cli/seeds.py` and run `pts seeds seattle
   --check-columns` (it validates the map against the live columns, then
   updates the row and increments `config_version`; the old config stays in
   `pts config history`). For other jurisdictions, fix the dossier and
   `pts discovery register --replace` (see [discovery.md](discovery.md)).
3. `pts sync sync -j <id> --full --yes`. Each report records the
   `config_version` it used.

### A jurisdiction without an API

Set `"machine_readable": false` in its `detail_strategy` (re-register with
`--replace`). Socrata syncs then stop at once with a non-retryable
`source_unavailable` (saved as a failed run, exit 1). Load it from exports
instead: `pts sync sync -j <id> --local-file <export.json>`.

---

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `Jurisdiction '...' not found in registry` | Not registered | `pts seeds seattle` |
| `relation "jurisdictions" does not exist` (or `"permits"`) | Schema not migrated | `pts db migrate` |
| `Sync failed ...` with `source_unavailable` | The Socrata API is down or rejected the request | `retryable: true`: try again later; `false`: check `base_url`/`dataset_id`. Use `--local-file` meanwhile |
| `jurisdiction_slug_mismatch` in `pts db doctor` | Rows synced with a prefix before 2026-11-09 | Repair SQL in [ops.md](ops.md) |
| Many `unknown_status` warnings | Status map doesn't cover the source values | Add them to `status_map` and re-run the seed |
| `Embedding model unavailable` | `sentence-transformers` missing or model download failed | `uv sync`; set `HF_TOKEN`; permits are still stored, without embeddings |
| `Sync failed ...` with `schema_violation` | A needed source column is gone | See [Runbook](#a-source-column-was-renamed-or-removed) |
| `--full re-pulls the whole dataset ...` | `--full` without `--yes` | Add `--yes`, or drop `--full` |
| Slow first sync | Every description is embedded once (~9 min on CPU for Seattle); after a normalization change, changed descriptions are re-embedded once | Use `--limit` for trials |

---

## FAQ

**How do I know a sync succeeded?** The command exits 0, prints the counts with
no `Error:` line, and a new row appears in `pts sync sync-report`.

**What happens if I interrupt a sync?** The upsert is one transaction, so an
interrupted run writes nothing and saves no report. Run it again. (A run that
fails because the *source* failed is saved, with its error.)

**Can I sync several jurisdictions?** Run `pts sync sync` once per jurisdiction id.

**How do I check data quality?** Look at the report's warnings (SQL above) and at
per-permit warnings:
`SELECT warnings, count(*) FROM permits WHERE warnings <> '[]'::jsonb GROUP BY 1;`.
`pts db doctor` checks integrity (orphans, `raw_hash` mismatches); see [ops.md](ops.md).

---

## Search with embeddings

Sync stores a 384-dimensional embedding of each description in
`permits.description_embedding` (`all-MiniLM-L6-v2`). There is no vector index:
vector search is an exact scan (~0.4 s at 193k rows), because the ranking runs
inside the structured filters where an approximate index would drop matches
(see [schema.md](schema.md), query 6).

```bash
# Hybrid: trigram text + vector rankings inside the structured filters, fused with RRF
pts search hybrid "roof replacement" -j seattle-kingcounty-wa --limit 20
pts search hybrid "roof" -j seattle-kingcounty-wa --structured '{"status": "ISSUED", "min_cost": 10000}'

# Vector only (semantic similarity)
pts search vector "deck additions" -j seattle-kingcounty-wa

# Text only (trigram word similarity)
pts search text "deck" -j seattle-kingcounty-wa

# Permits similar to one permit (permit number or internal row id)
pts search similar --permit-id 7083363-CN -j seattle-kingcounty-wa --limit 10
```

`--structured` keys: `status`, `permit_class` (string or list), `min_cost`,
`max_cost`, `issued_after`. Every result satisfies every filter.

To check that embeddings were stored:

```sql
SELECT count(*) FILTER (WHERE description_embedding IS NOT NULL) AS embedded, count(*)
FROM permits WHERE jurisdiction_id = 'seattle-kingcounty-wa';
```

The natural-language query API (`pts query`, `POST /api/v1/query`) uses the same
retrieval; see [api.md](api.md). More on embeddings:
[vector-embed-how-to-guide.md](vector-embed-how-to-guide.md).

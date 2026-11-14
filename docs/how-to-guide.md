# Permit Trade Skills - User Guide

How to set up the project and use the discovery, registration and seed commands
(Stage 01) that put jurisdictions into the registry. For syncing permits see
[ingest-how-to-guide.md](ingest-how-to-guide.md); for search and the query API
see [api.md](api.md); for detail extraction see [detail.md](detail.md).

> **Current state (2026-11-09).** Seattle is set up with `pts seeds seattle`.
> Discovery works from catalog.data.gov or a local export, but its platform
> probes are shallow and nothing is validated at registration, so review a
> dossier's maps before registering it. Known limits are listed in
> [discovery.md](discovery.md#limits).

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [Database](#database)
4. [Quick Start](#quick-start)
5. [Seeds](#seeds)
6. [Discovery](#discovery)
7. [Registration](#registration)
8. [Exporting a jurisdiction's config](#exporting-a-jurisdictions-config)
9. [Reference](#reference)

---

## Overview

- **Seeds** register a known jurisdiction without any network call. Seattle
  (`seattle-kingcounty-wa`) is the only seed.
- **Discovery** searches catalog.data.gov for a jurisdiction's datasets, probes
  each candidate's platform, scores the candidates and writes a JSON *dossier*.
  It can also build a dossier from a downloaded JSON export (`--local-file`).
- **Registration** turns a dossier into a row in the `jurisdictions` table.

Every later stage (sync, search, detail extraction, skill inference) reads the
jurisdiction's config from that row.

![Discovery & Registration Flow](diagrams/discovery-registration.png)

*See detailed [Component Diagram](diagrams/discovery-registration.puml) for more technical details.*

---

## Installation

Python 3.14 and [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv is not installed
git clone <repository-url>
cd permit-trade-skills
uv sync
uv run pts --help
```

---

## Database

PostgreSQL 18 with pgvector, in Docker:

```bash
./script/pgvector.sh --start        # or: cd script && docker compose up -d
uv run pts db migrate               # creates the extensions (vector, pg_trgm) and all tables
```

If PostgreSQL already runs on port 5432 on the host, stop it first
(`sudo systemctl stop postgresql`) or change the port in `script/docker-compose.yml`.

Every command takes `--db-url`. Without it, the URL comes from
`PERMIT_TRADE_SKILLS_DB_URL` (shell or `.env`), else
`postgresql://postgres:postgres@localhost:5432/permit_trade_skills`.

`pts seeds seattle` and `pts discovery discover` also bring the schema up to date
themselves. Schema operations are described in [ops.md](ops.md).

---

## Quick Start

```bash
uv run pts db migrate
uv run pts seeds seattle
uv run pts sync sync -j seattle-kingcounty-wa --local-file data/wa-seattle-building-permits.json
```

Then see [ingest-how-to-guide.md](ingest-how-to-guide.md).

---

## Seeds

```bash
uv run pts seeds seattle [--output-config seattle.yaml] [--check-columns] [--db-url URL]
```

| Option | Short | Description |
|---|---|---|
| `--output-config` | `-o` | Also export the registered row as YAML |
| `--check-columns` | | Check the mapped columns against the live `columns.json` (needs the Seattle API) |
| `--db-url` | | Database URL |

Validates the seed (see [Registration](#registration)), then registers
`seattle-kingcounty-wa`. Running it again updates the same row in place and
increments `config_version`; `created_at` is kept and the replaced config is
saved (`pts config history`). Re-run it after changing the seed in
`src/permit_trade_skills/cli/seeds.py`. With `--check-columns` it currently
reports `workclass` and `numbuildings`, which are unverified guesses at the
Socrata API's column names.

What the seed stores:

| Column | Value |
|---|---|
| `id` | `seattle-kingcounty-wa` |
| `name` / `state` / `county` | `Seattle King County` / `WA` / `King County` |
| `platform_type` | `socrata` |
| `base_url` | `https://cos-data.seattle.gov` |
| `api_endpoint` | `https://cos-data.seattle.gov/api/views` |
| `dataset_id` / `dataset_name` | `76t5-zqzr` / `Building Permits` |
| `field_map` | canonical field → Seattle column, e.g. `permit_num → permitnum`, `status_current → statuscurrent`, `issue_date → issueddate`, `valuation → estprojectcost` (25 fields) |
| `status_map` | canonical status (lower-case) → the raw `statuscurrent` values, covering all 24 values in the export, e.g. `issued: [Issued, Phase Issued, Active]`, `completed: [Completed, Closed, Inspections Completed, Approved to Occupy]` |
| `detail_strategy` | `type: dataset_api`, SODA pagination, `timestamp_field: issueddate` (incremental-sync cursor column), `detail_url_rule: https://services.seattle.gov/portal/customize/LinkToRecord.aspx?altId={{permitnum}}` (detail URL for rows without a `link`), and under `detail` the detail-extraction config ([detail.md](detail.md)) |

---

## Discovery

```bash
uv run pts discovery discover -j "Seattle" -s WA [-o dossier.json] [-q QUERY] [-l LIMIT] [--local-file rows.json] [--db-url URL]
```

| Option | Short | Description |
|---|---|---|
| `--jurisdiction` | `-j` | Jurisdiction name (required) |
| `--state` | `-s` | State abbreviation (required) |
| `--output` | `-o` | Where to write the dossier (none: not saved) |
| `--query` | `-q` | Catalog search to run instead of the default name searches, e.g. `'"building permits" WA'` |
| `--limit` | `-l` | Maximum candidates to collect (default 1000) |
| `--local-file` | | Build the dossier from a JSON export instead of the catalog |
| `--evidence-dir` | | Where to mirror the dossier (default `evals/evidence`) |
| `--db-url` | | Database URL (the command runs migrations) |

Every run is recorded in `discovery_runs` (linked to the jurisdiction when you
register it) and mirrored to
`<evidence-dir>/<jurisdiction-id>/discovery/<date>-<run>.json` without the
candidates' raw catalog metadata. The command prints a ranked table (top 20)
and the top candidate's score breakdown. Step-by-step: [discovery.md](discovery.md).

### From catalog.data.gov

1. **Search.** CKAN `package_search` for `"<name>"`, `<name>` and
   `<name> permits` (or your `--query`), de-duplicated by package id. The state
   does not affect the default search. 429/5xx responses are retried
   (honouring `Retry-After`); each request is logged.
2. **Score** each candidate 0–1: title match 35 %, organization match 20 %,
   keywords (name, state, permit, building, construction, department) 20 %,
   platform 15 %, metadata completeness 10 %.
3. **Probe** the top 5 candidates' hosts: Socrata (`/api/views.json`; any `.gov`
   host counts as Socrata), CKAN (`/api/3/action/...`), generic (`/api`,
   `/data.json`), else `other`.
4. **Re-score** and select the top candidate.
5. **Save** the dossier to `--output`.

Candidates start with `platform_type: null`; a probe fills it in. The probes
only look at the host (not the dataset's rows or columns), and the dossier has
empty `field_mapping`/`status_mapping`: fill them in before registering, or use
`--local-file` on an export of the dataset to have them guessed.

### From a local file

With `--local-file`, the command reads a JSON array of permit rows and builds a
dossier with one candidate (`platform_type: socrata`, confidence 1.0), without
calling the catalog or probing. It guesses (`discovery/mapping.py`):

- **`field_mapping`** from the column names in the first 1,000 records, using a
  synonym table (e.g. `permitnum → permit_num`, `issueddate → issue_date`,
  `estprojectcost → valuation`, `contractorcompanyname → contractor_name`).
- **`status_mapping`** from every distinct value of the status column, by
  keyword rules into the canonical statuses (`cancel`/`withdr`/`denied` →
  canceled, `expir` → expired, `review`/`intake`/`application`/`correction`… →
  pending, `complete`/`closed`/`occupy` → completed, `inspection` →
  in_inspection, `issued`/`active` → issued). Values no rule matches are left
  out (they sync as `UNKNOWN`) and listed in the dossier's `notes`.

On the Seattle export both guesses equal the seed's maps. Review them before
registering another jurisdiction.

### Dossier

```json
{
  "jurisdiction_id": "seattle-wa",
  "jurisdiction_name": "Seattle",
  "candidates": [
    {
      "id": "<ckan package id>",
      "title": "...",
      "organization": "...",
      "url": "<dataset landing page>",
      "platform_type": "socrata",
      "platform_detected_by": "socrata_domain_check",
      "confidence": 0.82,
      "score_breakdown": { "title_match": { "score": 1.0, "weight": 0.35, "weighted": 0.35 }, "...": "" },
      "identifier": "https://data.example.gov/api/views/abcd-1234",
      "publisher": "data.example.gov",
      "description": "...",
      "distributions": [ { "format": "CSV", "downloadURL": "...", "describedBy": null } ],
      "metadata": { "...": "the full CKAN package" },
      "evidence": {}
    }
  ],
  "verification_results": [
    { "probe_name": "socrata_domain_check", "success": true, "detected_platform": "socrata",
      "confidence": 0.75, "details": { "domain": "..." }, "error_message": null }
  ],
  "selected_candidate": { "...": "the top candidate" },
  "field_mapping": {},
  "status_mapping": {},
  "detail_strategy": {},
  "notes": "Selected candidate: ...",
  "discovery_run_id": "<uuid>",
  "created_at": "2026-11-09T12:00:00"
}
```

The jurisdiction id is derived as `<name lower-cased, spaces → hyphens>-<state>`
(e.g. `seattle-wa`); it does not include the county.

---

## Registration

```bash
uv run pts discovery register -d dossier.json -n "Seattle" -s WA [-c "King County"] [--dry-run] [--db-url URL]
```

Validates, then registers the dossier's selected (or `--approve`d) candidate as the jurisdiction `jurisdiction_id`
from the dossier, using the dossier's `field_mapping`, `status_mapping` and
`detail_strategy`, saves the probe results to the `probes` table and links the
discovery run. The
candidate's URL becomes `api_endpoint`, its id `dataset_id`, its title
`dataset_name`; `base_url` is left empty.

`pts register jurisdiction` is the same command under another name.

If the id is already registered, the command refuses (exit 1) and changes
nothing. Pass `--replace` to overwrite its field map, status map and detail
strategy; `config_version` is then incremented and the old config kept.
(`pts seeds seattle` always updates the Seattle row in place.)

| Option | Short | Description |
|---|---|---|
| `--dossier` | `-d` | Dossier JSON (required) |
| `--name` | `-n` | Jurisdiction name (required) |
| `--state` | `-s` | State abbreviation (required) |
| `--county` | `-c` | County name |
| `--approve` | | Register this candidate id instead of the top-ranked one |
| `--replace` | | Overwrite an already registered jurisdiction |
| `--dry-run` | | Print the planned action, config, candidate, probes and checks; write nothing |
| `--force` | | Register despite validation errors |
| `--check-columns` | | Check mapped columns against the dataset's live `columns.json` (needs `base_url` and `dataset_id`; dossier registrations have no `base_url`, so it is skipped with a warning) |
| `--db-url` | | Database URL |

Validation errors (refused unless `--force`): no `permit_num` mapping, a
status-map key that isn't a canonical status, status values that aren't lists
of strings, `detail_url_rule` placeholders that aren't mapped columns, and with
`--check-columns` mapped columns missing from the dataset. Warnings (printed,
not blocking): unknown field-map keys, unmapped `status_current`/`description`/
`issue_date`/`address`, an empty status map, duplicate raw statuses, a missing
or unmapped `timestamp_field`. A dossier straight from the catalog has empty
maps, so it is refused until you fill them in. Full list: [discovery.md](discovery.md).

---

## Exporting a jurisdiction's config

```bash
uv run pts config export -j seattle-kingcounty-wa              # -> config/jurisdictions/seattle-kingcounty-wa.yaml
uv run pts config history -j seattle-kingcounty-wa             # current and replaced versions
uv run pts config history -j seattle-kingcounty-wa --version 2 # one version as YAML
```

`config export` writes the jurisdiction's database row as YAML (every column
except `id`; `updated_at` is written as `exported_at`; `-o` for another path).
`config history` lists each replaced version with when and by which command it
was replaced (`jurisdiction_versions`; `api_key` is not copied), then the
current one. Both exit 1 for an unknown jurisdiction or version. Replacements
made before migration `0006` were not recorded.

Or query the table:

```bash
psql postgresql://postgres:postgres@localhost:5432/permit_trade_skills \
  -c "SELECT id, name, platform_type, dataset_id, config_version FROM jurisdictions WHERE is_active;"
```

---

## Reference

### Commands

```bash
pts discovery discover --help
pts discovery register --help
pts register jurisdiction --help
pts seeds seattle --help
pts config export --help
pts config history --help
```

### Environment variables

| Variable | Description | Default |
|---|---|---|
| `PERMIT_TRADE_SKILLS_DB_URL` | Database URL (shell or `.env`) | `postgresql://postgres:postgres@localhost:5432/permit_trade_skills` |
| `HF_TOKEN` | Hugging Face token for the embedding model download (sync) | unset |

### Platform types

| Type | Meaning |
|---|---|
| `socrata` | Socrata / SODA API (Seattle) |
| `ckan` | CKAN API |
| `other` | Anything else, or no platform detected |

### Tables

| Table | Written by | Contents |
|---|---|---|
| `jurisdictions` | seeds, register | One row per jurisdiction: identity, source, `field_map`, `status_map`, `detail_strategy`, `config_version`, `is_active` |
| `jurisdiction_versions` | seeds, register `--replace` | Each replaced config (`pts config history`) |
| `probes` | register | Platform probe results from the dossier |
| `discovery_runs` | discover (linked by register) | One row per discovery run |
| `status_maps` | nothing yet | Unused; the status map is the `jurisdictions.status_map` column |

The full schema is in [schema.md](schema.md).

### Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `connection refused` / `password authentication failed` | Database not running or wrong URL | `./script/pgvector.sh --status`; check `PERMIT_TRADE_SKILLS_DB_URL` |
| `Found 0 candidates` | The catalog is unreachable or has nothing under that name | Try another name, or use `--local-file` with an export |
| `... is already registered ...` | Registering an existing id | Pass `--replace` if you mean to overwrite its config |
| `the config has validation errors` | The dossier's maps would make sync store wrong data | Fix the maps (see the `error:` lines), or `--force` |
| Sync maps many permits to `UNKNOWN` | The status map doesn't list those raw values | Add them to `status_map` (see the dossier's `notes` for unmatched values) |

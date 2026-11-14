# Permit Trade Skills

Agentic RAG system for tracking building permits across U.S. jurisdictions.

It finds a city's open-data permit dataset, syncs the permits into PostgreSQL
(with pgvector embeddings), answers natural-language questions about them, fetches
each permit's official detail record, and infers the trade skills the permitted
work needs. Every inferred skill quotes the detail record word for word.

> **Status.** Early release. Seattle (`seattle-kingcounty-wa`) is the only
> jurisdiction set up out of the box; others can be added with
> `pts discovery`.

## Prerequisites

- **Docker** and Docker Compose (PostgreSQL 18 + pgvector runs in a container)
- **Python 3.14** and [uv](https://docs.astral.sh/uv/)
- **An LLM** for natural-language queries and trade-skill inference: an
  [OpenAI API key](https://platform.openai.com/api-keys), or a local
  [Ollama](https://ollama.com/) install
- About 1.5 GB of free disk for the Seattle data, the database and the embedding model
- Optional: Chromium for Playwright (`uv run playwright install chromium`), only
  for detail portals that render with JavaScript (Seattle does not)

## Quick start

### 1. Install

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # if uv is not installed
git clone https://github.com/tomoguara/permit-trade-skills.git
cd permit-trade-skills
uv sync
uv run pts --help
```

### 2. Configure the LLM

```bash
cp .env.example .env
```

**OpenAI:** in `.env`, set your key. `PTS_USE_LOCAL_OLLAMA_MODEL=false` is
already set, so every LLM role uses OpenAI:

```bash
OPENAI_API_KEY=sk-...
PTS_USE_LOCAL_OLLAMA_MODEL=false
```

**Or Ollama (local):** remove the `PTS_USE_LOCAL_OLLAMA_MODEL=false` line (the
default in [config/settings.yaml](config/settings.yaml) is Ollama), then pull
the model it names:

```bash
ollama pull qwen3.8:latest
```

How `PTS_USE_LOCAL_OLLAMA_MODEL` (shell or `.env`; the shell wins) picks the provider:

| Value | Provider |
|---|---|
| `false` (also `0`, `no`, `off`) | OpenAI |
| `true` (also `1`, `yes`, `on`) | Ollama |
| empty, or not set | `llm.use_local_ollama_model` in `config/settings.yaml` (default `true`: Ollama) |

Model names per role, prices and timeouts are in
[config/settings.yaml](config/settings.yaml).

### 3. Start the database

```bash
./script/pgvector.sh --start      # or: cd script && docker compose up -d
uv run pts db migrate             # extensions (vector, pg_trgm) and all tables
```

If PostgreSQL already runs on port 5432 on the host, stop it first
(`sudo systemctl stop postgresql`) or change the port in
[script/docker-compose.yml](script/docker-compose.yml) and
`PERMIT_TRADE_SKILLS_DB_URL` in `.env`.

### 4. Load the Seattle permits

```bash
uv run pts seeds seattle          # register seattle-kingcounty-wa (no network)

# Download the full dataset (~194k permits, ~200 MB, under a minute)
curl -o data/wa-seattle-building-permits.json \
  'https://data.seattle.gov/resource/76t5-zqzr.json?$limit=500000'

# Optional trial run: 1,000 permits, nothing written
uv run pts sync sync -j seattle-kingcounty-wa \
  --local-file data/wa-seattle-building-permits.json --limit 1000 --dry-run

# Sync everything
uv run pts sync sync -j seattle-kingcounty-wa \
  --local-file data/wa-seattle-building-permits.json
uv run pts sync sync-report -j seattle-kingcounty-wa --last 5
```

The first sync downloads the embedding model (`all-MiniLM-L6-v2`) from Hugging
Face and embeds every permit description. That takes about 9 minutes on CPU.
Re-running the sync is safe: an identical file is skipped, and otherwise only
new or changed permits are written.

Without `--local-file`, `pts sync sync` reads Seattle's live Socrata API and
pulls the permits applied for in the last 90 days. It is quicker to try, but the
API has been unreliable, and a failed pull is recorded as a failed run. See
[docs/ingest-how-to-guide.md](docs/ingest-how-to-guide.md).

### 5. Ask questions

```bash
uv run pts query "Which residential permits were issued in the last 90 days?"
uv run pts query 'issued permits over $100000 since 2026-07-01'   # single quotes around $
uv run pts permit show 7156276-DM                 # one permit as JSON
uv run pts search hybrid "roof replacement" -j seattle-kingcounty-wa   # keyword + semantic search

uv run pts api serve --port 8000                  # REST API, docs at http://127.0.0.1:8000/docs
```

See [docs/api.md](docs/api.md).

### 6. Fetch detail records and infer trade skills

```bash
uv run pts detail pick-live                       # a recently issued permit to try
uv run pts detail fetch -p 7083363-CN             # fetch and store its official record
uv run pts detail show -p 7083363-CN              # stored text, fields, provenance

uv run pts skills infer -p 7083363-CN             # cited trade skills for the permit
uv run pts skills show -p 7083363-CN              # each skill re-checked against the record (✓/✗)
```

Detail extraction fetches the Seattle permit portal page (falling back to the
Socrata row), checks that it belongs to the permit, and stores a citable
snapshot. Skill inference asks the LLM for the trade skills; a grounding check
drops any skill whose quote is not in the stored text. See
[docs/detail.md](docs/detail.md) and [docs/trade-skills.md](docs/trade-skills.md).

### 7. Run the pipeline, ask the agent, review

```bash
uv run pts pipeline run --dry-run --limit 10      # what would run: no LLM, no network, no writes
uv run pts pipeline run --limit 10                # detail -> skills for 10 issued permits
uv run pts agent goal "issued residential permits over \$100k in the last 90 days, with their trade skills"
export PTS_REVIEWER="Your Name"
uv run pts review list                            # inferences awaiting review
uv run pts review approve -p 7083363-CN
```

The pipeline worker drives issued permits through detail extraction and skill
inference with retries and a run report. The planner agent answers a question
by calling tools and returns a cited report, checked so that every permit and
skill in it came from a tool result. A person approves or rejects each
inference; the agent can only flag one for review. See
[docs/orchestration.md](docs/orchestration.md) and the runbook in
[docs/ops.md](docs/ops.md#orchestration-stage-07).

### 8. Measure quality and gate a build

```bash
uv run pts eval run -s H1 -s H4 -s H5 -s H7      # faithfulness, extraction, agent and pipeline integrity (no LLM)
uv run pts eval run -s H2 -s H3                  # trade recall on labelled permits, NL parsing (LLM, cached)
uv run pts eval run -s H6 --source-file data/wa-seattle-building-permits.json   # no permits lost in sync
uv run pts quality gate                          # PASS / CONDITIONAL / FAIL against config thresholds
uv run pts alerts check                          # drift detectors (nightly)
```

Each harness scores one property against stored data or the golden sets in
`evals/golden/seattle/`, and the gate decides whether a build may be
promoted (a hard criterion that was not run counts as a failure, so run all
seven suites before the gate). See [docs/quality.md](docs/quality.md) (what "good" means) and
[docs/quality_runbook.md](docs/quality_runbook.md) (labeling, incidents).

## CLI commands

Every command is `uv run pts <group> <subcommand> [OPTIONS]`, and every
subcommand takes `--help`. "DB" means the database from step 3.

| Command | Subcommands | What it does | Needs | Docs |
|---|---|---|---|---|
| `pts discovery …` | `discover`, `register` | Add a new city or county. `discover` searches catalog.data.gov (or reads a downloaded export with `--local-file`), probes and scores the candidate datasets, and writes a JSON dossier with guessed field and status maps. `register` checks the dossier and saves it as a jurisdiction. | DB; network for catalog.data.gov | [docs/discovery.md](docs/discovery.md) |
| `pts register …` | `jurisdiction` | Same as `pts discovery register`, under another name. | DB | [docs/how-to-guide.md](docs/how-to-guide.md#registration) |
| `pts seeds …` | `seattle` | Register or update the built-in Seattle jurisdiction (field map, status map, detail config) without any network call. Safe to re-run. | DB | [docs/how-to-guide.md](docs/how-to-guide.md#seeds) |
| `pts config …` | `export`, `history` | Export a jurisdiction's config to YAML, or list and print its earlier config versions. | DB | [docs/how-to-guide.md](docs/how-to-guide.md#exporting-a-jurisdictions-config) |
| `pts sync …` | `sync`, `sync-report` | Pull permits from a jurisdiction's Socrata API (incremental, 90-day window by default) or from a local export into the canonical store, normalize statuses, and embed descriptions. `sync-report` shows recent runs: counts, cursors, status changes and warnings. | DB; network for Socrata; embedding model | [docs/ingest-how-to-guide.md](docs/ingest-how-to-guide.md) |
| `pts search …` | `hybrid`, `vector`, `text`, `similar` | Search stored permits by structured filters, trigram keyword match, semantic similarity, or a mix fused with RRF. `similar` finds permits like a given one. | DB; embedding model | [docs/vector-embed-how-to-guide.md](docs/vector-embed-how-to-guide.md) |
| `pts query "…"` | (none) | Answer a natural-language question: the LLM turns it into a structured query, the code runs it and prints the rows with an echo of what ran. Same service as `POST /api/v1/query`. | DB; LLM | [docs/api.md](docs/api.md#cli-pts-query) |
| `pts api …` | `serve`, `openapi` | Run the REST query API (search, status counts, one permit, jurisdictions, natural-language query) with uvicorn, or write its OpenAPI document. | DB (`serve`); LLM for `/query` | [docs/api.md](docs/api.md) |
| `pts permit …` | `show` | Print one permit as JSON: the stored record, status history, detail snapshot and skill inference. `--raw` fetches the source open-data row live. | DB; network for `--raw` | [docs/api.md](docs/api.md#one-permit) |
| `pts detail …` | `fetch`, `show`, `report`, `revalidate`, `pick-live` | Fetch the official detail record for one permit or a cohort, check that it belongs to that permit, and store a citable snapshot. Also: inspect snapshots, summarize a fetch run, re-check stored content offline, and pick a permit to try. | DB; network for `fetch`; Chromium for JS portals | [docs/detail.md](docs/detail.md) |
| `pts skills …` | `infer`, `batch`, `show` | Infer the trade skills an issued permit's work needs from its detail snapshot. Each skill quotes the snapshot word for word, and skills whose quote is not in the text are dropped. `batch` runs every `DETAIL_READY` permit. | DB; LLM | [docs/trade-skills.md](docs/trade-skills.md) |
| `pts pipeline …` | `run`, `plan`, `status` | Drive every eligible issued permit through detail extraction and skill inference: concurrent, with retries and backoff, a time budget, SIGTERM draining and a run report. `plan` (= `run --dry-run`) shows what would run; `status` shows a run or a dashboard. | DB; network; LLM | [docs/orchestration.md](docs/orchestration.md) |
| `pts agent …` | `goal`, `replay` | Answer a permit question with the planner agent (tool calls under hard budgets, a cited report checked against the tool results). `replay` re-runs a recorded run's tool calls read-only and shows what changed. | DB; LLM; network for detail fetches | [docs/orchestration.md](docs/orchestration.md) |
| `pts review …` | `list`, `inspect`, `approve`, `reject`, `needs-human` | Review skill inferences: approve or reject (note required), or flag for review; bulk approve needs `--yes`. Needs `PTS_REVIEWER`. | DB | [docs/orchestration.md](docs/orchestration.md#human-review) |
| `pts eval …` | `run`, `judge`, `slo`, `diagnose`, `results`, `metrics` | Run the evaluation harnesses H1-H7 (results cached and tagged with a build id), the LLM judge on a sample (it can only flag rows for review), the SLO report, root-cause triage of a failing criterion, past results, and the Prometheus metrics. | DB; LLM for H2, H3, `judge` | [docs/quality_runbook.md](docs/quality_runbook.md) |
| `pts label …` | `list`, `show`, `annotate`, `judge`, `checklist`, `nl`, `import`, `export`, `verify` | Build the golden sets: label a permit's required trades (two labelers, then adjudication), extraction checklists and NL questions, export them to `evals/golden/`, and check their pinned input hashes. Needs `PTS_LABELER` or `--labeler`. | DB | [docs/quality_runbook.md](docs/quality_runbook.md#labeling) |
| `pts quality …` | `gate`, `show`, `signoff` | The release gate: check a build's newest results against the thresholds (exit 0 PASS, 1 FAIL, 2 CONDITIONAL); an owner signs off a CONDITIONAL build. | DB | [docs/quality.md](docs/quality.md) |
| `pts alerts …` | `check`, `list`, `ack` | Drift detectors (degraded snapshots, confidence shift, UNKNOWN statuses, H2 recall) that write alerts with a suspected cause. | DB | [docs/quality_runbook.md](docs/quality_runbook.md#nightly-monitoring) |
| `pts db …` | `migrate`, `upgrade`, `downgrade`, `status`, `revision`, `doctor`, `backup`, `prune` | Manage the schema (migrations, with destructive revisions refused by default), run integrity checks, back up with `pg_dump`, and apply the retention policy. | DB; `pg_dump` for `backup` | [docs/ops.md](docs/ops.md) |

## Configuration

| Setting | Where | Default |
|---|---|---|
| Database URL | `--db-url`, else `PERMIT_TRADE_SKILLS_DB_URL` (shell or `.env`) | `postgresql://postgres:postgres@localhost:5432/permit_trade_skills` |
| LLM provider | `PTS_USE_LOCAL_OLLAMA_MODEL`, else `llm.use_local_ollama_model` in [config/settings.yaml](config/settings.yaml) | Ollama |
| OpenAI key | `OPENAI_API_KEY` (shell or `.env`) | unset |
| Hugging Face token | `HF_TOKEN` (optional; authenticated model downloads) | unset |
| Reviewer name (`pts review`, `pts quality signoff`, `pts alerts ack`) | `PTS_REVIEWER` (shell or `.env`) | unset |
| Labeler name (`pts label`) | `PTS_LABELER` (shell or `.env`), or `--labeler` | unset |
| Drift alert webhook | `PTS_ALERT_WEBHOOK_URL`, else `eval.drift.webhook_url` | unset |
| Sync, detail, skill, orchestration and evaluation settings (thresholds) | [config/settings.yaml](config/settings.yaml) (or `$PTS_SETTINGS`) | see the file |

Secrets go only in the shell or `.env` (git-ignored), never in `settings.yaml`.

## Managing the database

```bash
./script/pgvector.sh --stop       # stop the container (data kept)
./script/pgvector.sh --clean      # stop and delete all data
uv run pts db status              # current and pending migrations
uv run pts db doctor              # integrity checks
```

Backups, retention and schema changes are in [docs/ops.md](docs/ops.md); the
schema and its ER diagram are in [docs/schema.md](docs/schema.md).

## Running the tests

```bash
./script/pgvector.sh --start
uv run pytest -q
```

The tests create and migrate their own `permit_trade_skills_test` database
(override it with `PTS_TEST_DB_URL`). They need no network and no LLM.

## Documentation

| Doc | Covers |
|---|---|
| [docs/how-to-guide.md](docs/how-to-guide.md) | Installation, seeds, discovery, registration, config export |
| [docs/discovery.md](docs/discovery.md) | Runbook for adding a new jurisdiction |
| [docs/ingest-how-to-guide.md](docs/ingest-how-to-guide.md) | Syncing permits, normalization, sync reports |
| [docs/vector-embed-how-to-guide.md](docs/vector-embed-how-to-guide.md) | Embeddings and `pts search` |
| [docs/api.md](docs/api.md) | REST API, natural-language queries, retrieval, LLM tools |
| [docs/detail.md](docs/detail.md) | Detail extraction |
| [docs/trade-skills.md](docs/trade-skills.md) | Trade-skill inference and the grounding rule |
| [docs/orchestration.md](docs/orchestration.md) | Pipeline worker, planner agent, human review |
| [docs/quality.md](docs/quality.md) | Quality Definition: metrics, thresholds, hard vs soft, golden sets |
| [docs/quality_runbook.md](docs/quality_runbook.md) | Evaluation, release gate, drift alerts, incidents, labeling |
| [docs/schema.md](docs/schema.md) | Database schema |
| [docs/ops.md](docs/ops.md) | Database and pipeline operations |

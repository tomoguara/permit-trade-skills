# Quality runbook

How to run the evaluation, gate a build, label golden data and handle a
quality incident. The definitions and thresholds are in
[quality.md](quality.md). Commands assume the repository root and a migrated
database (`uv run pts db migrate`); prefix them with `uv run`.

## Architecture overview

For a high-level architectural overview of the Quality, Evaluation & Monitoring
layer, including the evaluation module components, golden sets, release gate,
drift detectors, and monitoring infrastructure, see the
[Component Architecture](diagrams/quality-component.png) diagram in the
[Quality Definition](quality.md#component-architecture) document.

## Evaluate and gate a build

```bash
BUILD=$(git rev-parse --short HEAD)          # the default --build
pts eval run -s H1 -s H4 -s H5 -s H7 --build $BUILD           # no LLM calls
pts eval run -s H6 --source-file data/wa-seattle-building-permits.json --build $BUILD
pts eval run -s H2 -s H3 --build $BUILD      # LLM calls (cached per input + model + prompt version)
pts eval judge --build $BUILD                # advisory; flags rows NEEDS_HUMAN
pts quality gate --build $BUILD              # exit 0 PASS, 1 FAIL, 2 CONDITIONAL
```

- `--evidence` also writes each result to `evals/evidence/<dir>/eval/`.
- Re-running a suite with the same inputs, model and prompt version makes no
  LLM call (cache hit) and gives the same aggregate; `--force` re-calls.
- H6 against the live source: `pts eval run -s H6 --socrata`.
- A CONDITIONAL gate is promotable only after an owner sign-off:
  `PTS_REVIEWER=<name> pts quality signoff --build $BUILD --note "<why>"`.
  `pts quality show --build $BUILD` prints the stored report and sign-off.
- `--skip <criterion|suite>` excludes a criterion; it is recorded, and a
  skipped hard criterion makes the gate CONDITIONAL at best.

## Nightly monitoring

Run after the daily sync and pipeline (cron, as in [ops.md](ops.md)):

```cron
30 3 * * *  cd /srv/pts && uv run pts eval run -s H1 -s H4 -s H5 -s H7 --build nightly >> /var/log/pts/eval.log 2>&1
45 3 * * *  cd /srv/pts && uv run pts alerts check >> /var/log/pts/alerts.log 2>&1
```

`pts alerts check` exits 3 when it raised an alert; each alert is a
`drift_alerts` row and, when `eval.drift.webhook_url` (or
`PTS_ALERT_WEBHOOK_URL`) is set, a JSON POST. Detectors:

| Detector | Breach | Suspected cause |
|---|---|---|
| `degraded_share` | degraded detail snapshots rose > 20 points vs the previous 7 days | `detail_extraction`: the portal page probably changed |
| `confidence_shift` | KS test p < 0.01 on overall confidence | `model_change` (model/prompt mix changed), else `detail_extraction` or `model_or_detail_pages` |
| `unknown_status` | UNKNOWN status share rose > 2 points | `field_mapping`: a new raw status or a renamed column |
| `h2_recall` | newest H2 recall < 0.80 | `model_or_labels` |

`pts alerts list --open` shows unacknowledged alerts;
`PTS_REVIEWER=<name> pts alerts ack --id N` acknowledges one. The SLO report
is `pts eval slo --window 24h`.

## Incident: "skill lists look wrong since <timestamp>"

Target: root cause in under 5 minutes.

1. **Triage the window.** `pts eval diagnose --criterion H2 --window 7d`
   (or `H4`, `H1`, `judge`). For the worst items it prints the detail
   excerpt, the output skills, the missing trades, the judge's one-liner and
   a root cause: `source_page_changed`, `detail_extraction`, `model_change`
   or `model`. If there is no fresh run, run the suite first.
2. **Check for the portal-redesign signature.** `pts alerts list --open`: a
   `degraded_share` alert, and in the diagnosis "page-changed signature"
   (new snapshots degraded or failing must-appear lines while older ones are
   fine), mean the detail page changed.
3. **Page changed → fix the registry, not the model.** Update the
   jurisdiction's detail strategy (`detail_url_rule`, fetch strategy, field
   labels; see [detail.md](detail.md)), check one permit with
   `pts detail revalidate -p <permit>`, re-fetch the cohort with
   `pts detail fetch --since <date> --force`, then re-run H4.
4. **Model changed → pin the last known-good model and prompt version.**
   Every inference row stores `model` and `prompt_version`, so a rollback is
   a config change (`llm.providers.*.models.skills.infer` in
   `config/settings.yaml`), not a redeploy. Compare runs with
   `pts eval results --suite H2`.
5. **Re-run H1-H5 on the affected cohort** (`--since <date>`) and the gate.
6. **Record the incident**: the diagnosis output and the remedy go in the
   incident ticket and, as evidence, under `evals/evidence/<dir>/eval/`
   (`pts eval run ... --evidence`).

Table-top drill: seed the "portal changed" cohort from
`tests/integration/test_eval_stage08.py::test_portal_changed_cohort_raises_a_drift_alert_and_diagnosis_points_at_the_page_ac5`
on a scratch database and have a second engineer reach step 3 from
`pts alerts list` and `pts eval diagnose` alone.

## Labeling

Set your name once: `export PTS_LABELER=<name>`.

| Task | Command |
|---|---|
| See the queue | `pts label list --candidates` |
| Read a permit's input | `pts label show -p 7170078-CN [--full]` |
| Label a trade set (interactive) | `pts label annotate -p 7170078-CN` |
| Label with flags | `pts label annotate -p 7170078-CN --required Carpentry,Plumbing,Electrical --optional Drywall --unrequired Roofing --skill "Plumbing=Kitchen sink supply and drain piping" --difficulty medium` |
| Adjudicate (not a labeler of that permit; name from `--adjudicator`, `--labeler` or `PTS_LABELER`) | `pts label judge -p 7170078-CN --labeler <name> --note "union per policy" [--drop "Electrical=no electrical scope"] [--single-labeler]` |
| Extraction checklist (Dataset C) | `pts label checklist -p 7170078-CN --line 7170078-CN --line "7316 21ST AVE NE" --line "STATUS: Issued" --line "kitchen"` |
| Pin NL questions (Dataset B) | `pts label nl -f questions.jsonl --as-of 2026-11-10` |
| Import draft labels | `pts label import drafts.jsonl` |
| Export the golden files | `pts label export` |
| Verify hashes | `pts label verify` (exit 1 when a row is SUPERSEDED) |

Rules:

- Two independent labelers per Dataset A permit, then an adjudicator who is
  neither. Gold = union of the required trades; dropping one needs a note.
- **Separation of duties**: a labeler never labels a permit whose inference
  they reviewed (`pts label annotate` refuses).
- **Rotation**: every week, re-label 2 of the 10 most recently issued
  permits (`pts label list --candidates`) so the gold stays fresh; the
  `h2_recall` detector watches the newest H2 run.
- A changed detail page (new snapshot) supersedes the labels made against
  the old one; re-label it rather than editing the file.
- Draft labels from an AI labeler are stored as `labeled_by: draft-<name>`;
  they are one labeler, never the adjudicator. `pts label export --provisional`
  writes unadjudicated labels to `labels/provisional/` for a baseline run
  (`pts eval run -s H2 --golden-dir <that dir>`); the gold file only ever
  contains adjudicated rows.

## Evidence retention

Every `evals/evidence/...` file (Stages 04-08) is kept at least one year for
audit and model-change forensics: it is committed to the repository, and
`pts db prune` never touches `eval_results`, `build_gates`, `drift_alerts`
or `labels`.

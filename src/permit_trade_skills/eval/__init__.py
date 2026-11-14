"""Stage 08: evaluation and quality (the "does it actually work" layer).

Makes quality measurable, replayable and gating:

- labels.py: golden-set labeling (Datasets A trade sets, B NL queries,
  C extraction checklists) with pinned input hashes; `pts label`.
- trades.py: the canonical trade vocabulary used by labels and H2 scoring.
- harnesses.py: H1-H7 (`pts eval run --suite <name>`), each emitting
  per-item rows and an aggregate into `eval_results`; LLM results are cached
  by (suite, pinned input hash, model, prompt version).
- judge.py: LLM-as-judge (role `eval.judge`), advisory; a 0 on "evidence
  supports skill" re-runs the grounding check and flags the row NEEDS_HUMAN
  (never an auto-reject).
- gate.py: the release gate (`pts quality gate --build <id>`).
- drift.py, diagnose.py, slo.py, metrics.py: monitoring, drift alerts,
  root-cause triage, the SLO report and Prometheus `/metrics`.

This package never imports `permit_trade_skills.store` or SQLAlchemy; it
talks to `eval.ports.EvalStore`, implemented by `store/eval.SqlEvalStore` and
wired in `permit_trade_skills.wiring`. See docs/quality.md (the Quality
Definition) and docs/quality_runbook.md.
"""

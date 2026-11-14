"""Stage 07: agentic orchestration.

- `planner`: the planner agent (role `agent.planner`), a JSON next-action loop
  over the closed `toolset-v1` (`tools`), with hard budgets (`budgets`) and the
  no-fabrication report validator (`validator`). Prompt: prompts/planner_v1.md.
- `pipeline`: the durable worker that drives eligible permits through detail
  extraction and skill inference, with retries (`retry_backlog`), concurrency,
  a wall budget and SIGTERM draining.
- `review`: the human-in-the-loop review (agents can only propose).
- `states`: the lifecycle legal-edge table; `events`: the orchestration_events
  trail; `replay`: drift detection over a recorded agent run; `costs`: LLM
  call metering; `ports`: the persistence Protocol.

This package never imports `permit_trade_skills.store` and uses only
`permit_trade_skills.skills` from Stage 06; build wired objects with
`permit_trade_skills.wiring` (`planner()`, `pipeline_worker()`,
`review_service()`). See docs/orchestration.md and docs/ops.md.
"""

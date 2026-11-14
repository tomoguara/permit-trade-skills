"""The orchestration event trail: one `run_id` per pipeline run or agent goal.

`EventLog.record` appends an `orchestration_events` row through the port and
mirrors it as a structured JSON log line (logger `pts.orchestration`; the
table is the source of truth, the log omits result bodies). A failed event
write is logged and does not stop a run.

Event names:
  agent.start / agent.finish / agent.integrity_failed   planner runs
  tool.call                                             one planner tool call (params_json, result_json)
  pipeline.start / pipeline.finish                      worker runs
  step.detail / step.infer                              one worker step attempt
  transition                                            a processing_state change (from_state, to_state)
  retry.scheduled / retry.exhausted                     cross-run retry bookkeeping
  review.decision                                       a human decision or an agent proposal
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from permit_trade_skills.orchestration.ports import OrchestrationStore

log = logging.getLogger("pts.orchestration")

# The run a tool call belongs to (set by the planner for the duration of a goal).
CURRENT_RUN: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("pts_orchestration_run",
                                                                              default=None)


def current_run_id() -> Optional[str]:
    return CURRENT_RUN.get()


def utcnow() -> datetime:
    """Naive UTC, matching the schema's DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def jsonable(value: Any) -> Any:
    """Round-trip through JSON (datetimes and other objects become strings)."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    return str(value)


class EventLog:
    def __init__(self, store: OrchestrationStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def record(self, event: str, *, permit_id: Optional[int] = None, from_state: Optional[str] = None,
               to_state: Optional[str] = None, tool: Optional[str] = None, params: Any = None,
               result: Any = None, ok: bool = True, error_code: Optional[str] = None,
               retryable: Optional[bool] = None, latency_ms: Optional[int] = None) -> Dict[str, Any]:
        row = {"run_id": self.run_id, "permit_id": permit_id, "created_at": utcnow(), "event": event,
               "from_state": from_state, "to_state": to_state, "tool": tool,
               "params_json": jsonable(params), "result_json": jsonable(result), "ok": ok,
               "error_code": error_code, "retryable": retryable, "latency_ms": latency_ms}
        try:
            self.store.add_event(row)
        except Exception:  # noqa: BLE001 - the trail must not take a run down
            log.exception("orchestration.event_write_failed run=%s event=%s", self.run_id, event)
        line = {k: v for k, v in row.items() if v is not None and k != "result_json"}
        line["created_at"] = row["created_at"].isoformat() + "Z"
        log.info(json.dumps({"event": f"orchestration.{event}", **{k: v for k, v in line.items() if k != "event"}},
                            default=_default))
        return row

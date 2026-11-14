"""The durable pipeline worker: detail extraction -> skill inference for every eligible permit.

Eligibility (one query per run, `OrchestrationStore.eligible_permits`): the
jurisdiction's permits whose `status` is one Stage 06 accepts
(`skills.allowed_statuses`, default ISSUED) and whose `processing_state` is
SYNCED, DETAIL_READY, DETAIL_FAILED or SKILLS_FAILED, newest issued first,
with `--limit`, `--since` (issued on/after) and `--permit`. Excluded: permits
whose retry is not yet due or whose attempts are exhausted (`retry_backlog`),
and SKILLS_FAILED permits whose failure is not retryable (they stay
SKILLS_FAILED and are reported). `--permit` ignores the backlog.

Per permit (see states.py for the edge table):

  SYNCED / DETAIL_FAILED --detail ok--> DETAIL_READY --infer ok--> SKILLS_INFERRED
  detail failed, retryable      -> DETAIL_FAILED, retry queued (backlog)
  detail failed, not retryable  -> DETAIL_FAILED -> SKILLS_SKIPPED (orchestrator edge, reason kept)
  infer failed, retryable       -> SKILLS_FAILED, retry queued
  infer failed, not retryable   -> SKILLS_FAILED (stable; GROUNDING_FAILED, NO_SKILLS)
  not eligible for inference    -> DETAIL_READY -> SKILLS_SKIPPED
  DETAIL_FAILED, not retryable, found in the queue -> SKILLS_SKIPPED without a fetch

Stage 05/06 write their own state; the worker observes every change and
records a `transition` event (an edge outside states.EDGES is flagged
ILLEGAL_TRANSITION). The worker's own writes go through `states.check` first.

Retries: up to `attempts_per_run` (2) attempts per step in one run, then the
retry moves to a later run; `max_attempts` (3) in all, with backoff
`backoff_s[attempt - 1]` (30 s, 2 min) before the next attempt. A retry is a
`retry_backlog` row (`next_retry_at`); exhausted rows stay `*_FAILED`.
`permits.processing_state` + `retry_backlog` are the durable queue: a run
that stops leaves the rest for the next run, and nothing is duplicated
(Stage 06 stores one inference per input cache key).

Concurrency: `workers` permits at a time on one event loop. Detail fetches
share one Stage 05 pipeline (its per-host rate limiter and CAPTCHA
cool-downs); inference runs in threads. `max_minutes` stops scheduling and
lets in-flight permits finish. SIGTERM/SIGINT (CLI) stop scheduling, wait
`drain_s` (30 s) for in-flight permits, then cancel them; an LLM call already
in flight cannot be interrupted, so the process exits when it returns.

Every step attempt is a `step.detail` / `step.infer` event; every permit ends
with a `permit.done` event; the run report is a `pipeline_runs` row.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from permit_trade_skills.orchestration import states as S
from permit_trade_skills.orchestration.costs import delta
from permit_trade_skills.orchestration.events import EventLog, new_run_id, utcnow
from permit_trade_skills.orchestration.ports import OrchestrationStore
from permit_trade_skills.skills import InferenceFailed, NotEligible

log = logging.getLogger("pts.orchestration")

ELIGIBLE_STATES = (S.SYNCED, S.DETAIL_READY, S.DETAIL_FAILED, S.SKILLS_FAILED)
DETAIL, INFER = "detail", "skill_infer"
TIMEOUT, INTERNAL, NOT_ELIGIBLE, PERMIT_NOT_FOUND = "TIMEOUT", "INTERNAL", "NOT_ELIGIBLE", "PERMIT_NOT_FOUND"
SIGNAL_REASONS = ("SIGTERM", "SIGINT")

DEFAULT_PIPELINE_SETTINGS: Dict[str, Any] = {
    "workers": 4, "max_workers": 16, "max_minutes": 60, "limit": 25, "max_attempts": 3, "attempts_per_run": 2,
    "backoff_s": [30, 120], "detail_timeout_s": 120, "infer_timeout_s": 120, "drain_s": 30,
}
COMPLETED_OUTCOMES = ("inferred", "cached")
FAILED_OUTCOMES = ("retry", "exhausted", "failed", "error")


@dataclass
class _Run:
    run_id: str
    jurisdiction: str
    events: EventLog
    deadline: float
    stop: asyncio.Event
    hard_stop: asyncio.Event = field(default_factory=asyncio.Event)
    pipe: Any = None
    config: Any = None
    refs: Dict[str, Any] = field(default_factory=dict)
    illegal: List[Dict[str, Any]] = field(default_factory=list)


class PipelineWorker:
    def __init__(self, store: OrchestrationStore, detail_service: Any, skill_service: Any, *,
                 settings: Optional[Dict[str, Any]] = None, eligible_statuses: Sequence[str] = ("ISSUED",),
                 meter: Any = None, sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = utcnow,
                 on_permit_done: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.store = store
        self.detail = detail_service
        self.skills = skill_service
        self.settings = {**DEFAULT_PIPELINE_SETTINGS, **(settings or {})}
        self.statuses = tuple(eligible_statuses)
        self.meter = meter
        self.sleep = sleep
        self.clock = clock
        self.now = now
        self.on_permit_done = on_permit_done
        self._run: Optional[_Run] = None
        self.stop_reason: Optional[str] = None

    # ---- cohort and plan -------------------------------------------------------

    def cohort(self, jurisdiction: str, *, limit: Optional[int] = None, since: Optional[datetime] = None,
               permit_numbers: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        limit = int(limit if limit is not None else self.settings["limit"])
        return self.store.eligible_permits(jurisdiction, statuses=self.statuses, states=ELIGIBLE_STATES,
                                           since=since, permit_numbers=list(permit_numbers or []) or None,
                                           limit=limit, now=self.now(), ignore_backlog=bool(permit_numbers))

    def _row_plan(self, row: Dict[str, Any], detail_action: Optional[str]) -> Dict[str, Any]:
        state, err = row["processing_state"], row.get("state_error") or {}
        attempts = int(row.get("attempts") or 0)
        entry = {"permit_number": row["permit_number"], "status": row["status"], "state": state,
                 "issued_at": row["issued_at"].date().isoformat() if row.get("issued_at") else None,
                 "attempts": attempts, "max_attempts": self.settings["max_attempts"]}
        if state == S.DETAIL_FAILED and err.get("retryable") is False:
            entry.update(action="skip", steps=[], reason=f"DETAIL_FAILED not retryable ({err.get('error')})")
        elif state in (S.SYNCED, S.DETAIL_FAILED):
            entry.update(action="detail -> infer", steps=[f"detail: {detail_action or 'fetch'}", "infer"])
        else:
            entry.update(action="infer", steps=["infer" if state == S.DETAIL_READY else "infer (retry)"])
        if state in (S.DETAIL_FAILED, S.SKILLS_FAILED) and err:
            entry["last_error"] = err.get("error")
        return entry

    def plan(self, jurisdiction: str, *, limit: Optional[int] = None, since: Optional[datetime] = None,
             permit_numbers: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """--dry-run: the full plan; no LLM call, no network, no writes."""
        rows = self.cohort(jurisdiction, limit=limit, since=since, permit_numbers=permit_numbers)
        needs_detail = [r["permit_number"] for r in rows if r["processing_state"] in (S.SYNCED, S.DETAIL_FAILED)]
        detail_actions: Dict[str, str] = {}
        if needs_detail:
            refs = self.detail.cohort(jurisdiction, permit_numbers=needs_detail, limit=len(needs_detail))
            detail_actions = {p["permit_number"]: p["action"] for p in self.detail.plan(jurisdiction, refs)}
        entries = [self._row_plan(r, detail_actions.get(r["permit_number"])) for r in rows]
        return {"jurisdiction": jurisdiction, "dry_run": True, "statuses": list(self.statuses),
                "planned": len(entries), "llm_calls_max": sum(1 for e in entries if "infer" in e["action"]),
                "permits": entries}

    # ---- running -------------------------------------------------------------------

    def request_stop(self, reason: str = "stop") -> None:
        """Stop scheduling new permits (SIGTERM handler, tests, operators). In-flight permits finish;
        after a signal (SIGTERM/SIGINT) they get `drain_s` and are then cancelled."""
        hard = reason in SIGNAL_REASONS
        if hard or not self.stop_reason:
            self.stop_reason = reason
        if self._run is not None:
            self._run.stop.set()
            if hard:
                self._run.hard_stop.set()

    def run(self, jurisdiction: str, **kw: Any) -> Dict[str, Any]:
        return asyncio.run(self.arun(jurisdiction, **kw))

    async def arun(self, jurisdiction: str, *, limit: Optional[int] = None, since: Optional[datetime] = None,
                   permit_numbers: Optional[Sequence[str]] = None, workers: Optional[int] = None,
                   max_minutes: Optional[float] = None, trigger: str = "cli",
                   handle_signals: bool = False) -> Dict[str, Any]:
        workers = max(1, min(int(workers or self.settings["workers"]), int(self.settings["max_workers"])))
        max_minutes = float(max_minutes if max_minutes is not None else self.settings["max_minutes"])
        run_id = new_run_id("pipeline")
        params = {"limit": limit if limit is not None else self.settings["limit"],
                  "since": since.isoformat() if since else None, "permits": list(permit_numbers or []),
                  "workers": workers, "max_minutes": max_minutes, "statuses": list(self.statuses)}
        started, started_at = self.clock(), self.now()
        rows = self.cohort(jurisdiction, limit=limit, since=since, permit_numbers=permit_numbers)
        self.stop_reason = None
        ctx = _Run(run_id, jurisdiction, EventLog(self.store, run_id), started + max_minutes * 60, asyncio.Event())
        self._run = ctx
        self.store.create_pipeline_run(run_id, jurisdiction, trigger, params)
        ctx.events.record("pipeline.start", params={**params, "planned": len(rows),
                                                    "permits_planned": [r["permit_number"] for r in rows]})
        meter_before = self.meter.snapshot() if self.meter is not None else None
        loop = asyncio.get_running_loop()
        if handle_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_stop, sig.name)

        results: List[Dict[str, Any]] = []
        error: Optional[str] = None
        try:
            await self._open_detail(ctx, rows)
            results = await self._schedule(ctx, rows, workers)
        except Exception as e:  # noqa: BLE001 - close the run as failed, then report
            log.exception("pipeline.failed run=%s", run_id)
            error = f"{type(e).__name__}: {e}"
        finally:
            if handle_signals:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
            await self._close_detail(ctx, "failed" if error else "completed", error)
            self._run = None

        usage = delta(meter_before, self.meter.snapshot()) if meter_before is not None else None
        report = self._report(ctx, rows, results, usage, error, started, started_at, params)
        self.store.finish_pipeline_run(run_id, report)
        ctx.events.record("pipeline.finish", ok=report["status"] == "completed", error_code=report.get("error"),
                          result={k: v for k, v in report.items() if k != "results"},
                          latency_ms=report["duration_ms"])
        return report

    async def _open_detail(self, ctx: _Run, rows: List[Dict[str, Any]]) -> None:
        numbers = [r["permit_number"] for r in rows if r["processing_state"] in (S.SYNCED, S.DETAIL_FAILED)]
        if not numbers:
            return
        ctx.refs = {p.permit_number: p for p in self.detail.cohort(ctx.jurisdiction, permit_numbers=numbers,
                                                                   limit=len(numbers))}
        ctx.pipe = self.detail.pipeline()
        ctx.config = ctx.pipe.config(ctx.jurisdiction)
        self.detail.store.create_run(ctx.run_id, ctx.jurisdiction, "pipeline", {"permits": numbers})

    async def _close_detail(self, ctx: _Run, status: str, error: Optional[str]) -> None:
        if ctx.pipe is None:
            return
        try:
            await ctx.pipe.aclose()
        finally:
            self.detail.store.finish_run(ctx.run_id, status, error)

    async def _schedule(self, ctx: _Run, rows: List[Dict[str, Any]], workers: int) -> List[Dict[str, Any]]:
        sem = asyncio.Semaphore(workers)
        tasks: Dict[asyncio.Task, Dict[str, Any]] = {}
        queued: List[Dict[str, Any]] = []
        for row in rows:
            if not ctx.stop.is_set() and self.clock() >= ctx.deadline:
                self.request_stop("max_minutes")
            if ctx.stop.is_set() or not await self._acquire(sem, ctx.stop):
                queued.append(self._queued(row))
                continue
            task = asyncio.create_task(self._process_safe(ctx, row))
            task.add_done_callback(lambda _t: sem.release())
            tasks[task] = row
        results = await self._drain(ctx, tasks)
        return results + queued

    @staticmethod
    async def _acquire(sem: asyncio.Semaphore, stop: asyncio.Event) -> bool:
        acquire = asyncio.ensure_future(sem.acquire())
        stopper = asyncio.ensure_future(stop.wait())
        done, _ = await asyncio.wait({acquire, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if acquire in done:
            if stop.is_set():
                sem.release()
                return False
            return True
        acquire.cancel()
        try:
            await acquire
        except asyncio.CancelledError:
            return False
        sem.release()           # it was granted just before the cancel landed
        return False

    async def _drain(self, ctx: _Run, tasks: Dict[asyncio.Task, Dict[str, Any]]) -> List[Dict[str, Any]]:
        pending = set(tasks)
        while pending:
            if ctx.hard_stop.is_set():                  # a signal: drain for drain_s, then cancel
                _, pending = await asyncio.wait(pending, timeout=float(self.settings["drain_s"]))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break
            waiter = asyncio.ensure_future(ctx.hard_stop.wait())
            _, still = await asyncio.wait(pending | {waiter}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            pending = still - {waiter}
        out = []
        for task, row in tasks.items():
            if task.cancelled():
                out.append({**self._queued(row), "outcome": "interrupted"})
            else:
                out.append(task.result())
        return out

    def _queued(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {"permit_number": row["permit_number"], "outcome": "queued", "start_state": row["processing_state"],
                "final_state": row["processing_state"]}

    # ---- one permit ------------------------------------------------------------------

    async def _process_safe(self, ctx: _Run, row: Dict[str, Any]) -> Dict[str, Any]:
        out = {"permit_number": row["permit_number"], "start_state": row["processing_state"]}
        try:
            await self._process(ctx, row, out)
        except asyncio.CancelledError:
            out.update(outcome="interrupted")
            raise
        except Exception as e:  # noqa: BLE001 - one permit never takes the run down
            log.exception("pipeline.permit_error run=%s permit=%s", ctx.run_id, row["permit_number"])
            out.update(outcome="error", error=INTERNAL, message=f"{type(e).__name__}: {e}", retryable=True)
        out["final_state"] = self.store.permit_state(row["id"])[0]
        ctx.events.record("permit.done", permit_id=row["id"], params={"permit_number": row["permit_number"]},
                          result=out, ok=out.get("outcome") not in FAILED_OUTCOMES, error_code=out.get("error"))
        if self.on_permit_done is not None:
            self.on_permit_done(out)
        return out

    async def _process(self, ctx: _Run, row: Dict[str, Any], out: Dict[str, Any]) -> None:
        pid, state = row["id"], row["processing_state"]
        err = row.get("state_error") or {}
        if state == S.DETAIL_FAILED and err.get("retryable") is False:
            self._skip(ctx, row, S.DETAIL_FAILED, err, out)
            return
        if state in (S.SYNCED, S.DETAIL_FAILED):
            ok, info = await self._attempts(ctx, row, DETAIL, self._detail_attempt)
            if not ok:
                if info["outcome"] == "failed":       # not retryable: skip the permit
                    self._skip(ctx, row, self.store.permit_state(pid)[0], info, out)
                else:
                    out.update(info)
                return
        ok, info = await self._attempts(ctx, row, INFER, self._infer_attempt)
        out.update(info)
        if not ok and info.get("error") == NOT_ELIGIBLE:
            self._skip(ctx, row, self.store.permit_state(pid)[0], info, out)

    def _skip(self, ctx: _Run, row: Dict[str, Any], state: Optional[str], err: Dict[str, Any],
              out: Dict[str, Any]) -> None:
        reason = err.get("error") or "UNKNOWN"
        error = {"stage": "orchestration", "error": reason, "retryable": False,
                 "message": err.get("message") or f"skipped: {reason}", "cause": err}
        if state in (S.DETAIL_FAILED, S.DETAIL_READY) and self._advance(ctx, row["id"], state, S.SKILLS_SKIPPED, error):
            self.store.clear_backlog(row["id"])
            out.update(outcome="skipped", error=reason, retryable=False)
        else:
            out.update(outcome="failed", error=reason, retryable=False)

    def _advance(self, ctx: _Run, permit_id: int, from_state: str, to_state: str,
                 error: Optional[Dict[str, Any]] = None) -> bool:
        owner, event = S.check(from_state, to_state)     # IllegalTransition before any write
        if not self.store.advance_state(permit_id, from_state, to_state, error):
            return False
        ctx.events.record("transition", permit_id=permit_id, from_state=from_state, to_state=to_state,
                          params={"owner": owner, "event": event}, result={"state_error": error} if error else None)
        return True

    def _observe(self, ctx: _Run, permit_id: int, before: Optional[str]) -> Optional[str]:
        """Record a state change made by Stage 05/06 during a step (flag it if illegal)."""
        after, err = self.store.permit_state(permit_id)
        if after != before and before is not None and after is not None:
            legal = S.is_legal(before, after)
            owner, event = S.EDGES.get((before, after), (None, None))
            ctx.events.record("transition", permit_id=permit_id, from_state=before, to_state=after, ok=legal,
                              error_code=None if legal else "ILLEGAL_TRANSITION",
                              params={"owner": owner, "event": event},
                              result={"state_error": err} if err else None)
            if not legal:
                ctx.illegal.append({"permit_id": permit_id, "from": before, "to": after})
        return after

    async def _attempts(self, ctx: _Run, row: Dict[str, Any], stage: str,
                        attempt_fn: Callable[..., Any]) -> Tuple[bool, Dict[str, Any]]:
        """Run a step with in-run retries; book cross-run retries in retry_backlog."""
        prior = int(row.get("attempts") or 0) if row.get("stage") == stage else 0
        max_attempts = int(self.settings["max_attempts"])
        backoff = list(self.settings["backoff_s"]) or [0]
        attempt, info = prior, {}
        for i in range(int(self.settings["attempts_per_run"])):
            attempt = prior + i + 1
            ok, info = await attempt_fn(ctx, row, attempt)
            if ok:
                self.store.clear_backlog(row["id"])
                return True, info
            if not info.get("retryable"):
                self.store.clear_backlog(row["id"])
                return False, {**info, "outcome": "failed", "attempts": attempt}
            if attempt >= max_attempts or ctx.stop.is_set():
                break
            if i + 1 < int(self.settings["attempts_per_run"]):
                delay = float(backoff[min(attempt - 1, len(backoff) - 1)])
                if self.clock() + delay >= ctx.deadline or not await self._wait(delay, ctx.stop):
                    break
        exhausted = attempt >= max_attempts
        delay = float(backoff[min(attempt - 1, len(backoff) - 1)])
        next_retry = None if exhausted else self.now() + timedelta(seconds=delay)
        self.store.upsert_backlog(row["id"], {"stage": stage, "attempts": attempt, "next_retry_at": next_retry,
                                              "exhausted": exhausted, "last_error": info,
                                              "last_run_id": ctx.run_id})
        ctx.events.record("retry.exhausted" if exhausted else "retry.scheduled", permit_id=row["id"],
                          params={"permit_number": row["permit_number"], "stage": stage, "attempts": attempt},
                          result={"next_retry_at": next_retry, "last_error": info}, ok=False,
                          error_code=info.get("error"), retryable=not exhausted)
        return False, {**info, "outcome": "exhausted" if exhausted else "retry", "attempts": attempt,
                       "next_retry_at": next_retry.isoformat() + "Z" if next_retry else None}

    async def _wait(self, delay: float, stop: asyncio.Event) -> bool:
        """Sleep `delay` unless a stop is requested first (False)."""
        if delay <= 0:
            return not stop.is_set()
        sleeper = asyncio.ensure_future(self.sleep(delay))
        stopper = asyncio.ensure_future(stop.wait())
        done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        return sleeper in done and not stop.is_set()

    async def _detail_attempt(self, ctx: _Run, row: Dict[str, Any], attempt: int) -> Tuple[bool, Dict[str, Any]]:
        pid, number = row["id"], row["permit_number"]
        before = self.store.permit_state(pid)[0]
        ref = ctx.refs.get(number)
        started = self.clock()
        result: Dict[str, Any] = {}
        if ref is None:
            ok, info = False, {"error": PERMIT_NOT_FOUND, "retryable": False, "message": "permit not in the cohort"}
        else:
            try:
                outcome = await asyncio.wait_for(ctx.pipe.safe_process(ref, ctx.config, run_id=ctx.run_id),
                                                 float(self.settings["detail_timeout_s"]))
                ok = outcome.outcome in ("ok", "skipped")
                info = {} if ok else {"error": outcome.error, "retryable": bool(outcome.retryable),
                                      "message": "; ".join(outcome.notes[-3:])}
                result = {"outcome": outcome.outcome, "strategy": outcome.strategy,
                          "snapshot_id": outcome.snapshot_id, "degraded": outcome.degraded}
            except asyncio.TimeoutError:
                ok, info = False, {"error": TIMEOUT, "retryable": True,
                                   "message": f"detail step over {self.settings['detail_timeout_s']} s"}
        ctx.events.record("step.detail", permit_id=pid, params={"permit_number": number, "attempt": attempt},
                          result={**result, **info}, ok=ok, error_code=info.get("error"),
                          retryable=info.get("retryable"), latency_ms=int((self.clock() - started) * 1000))
        self._observe(ctx, pid, before)
        return ok, info

    async def _infer_attempt(self, ctx: _Run, row: Dict[str, Any], attempt: int) -> Tuple[bool, Dict[str, Any]]:
        pid, number = row["id"], row["permit_number"]
        before = self.store.permit_state(pid)[0]
        started = self.clock()
        result: Dict[str, Any] = {}
        try:
            inf = await asyncio.wait_for(asyncio.to_thread(self.skills.infer, number, ctx.jurisdiction),
                                         float(self.settings["infer_timeout_s"]))
            ok, info = True, {"outcome": "cached" if inf.cached else "inferred"}
            result = {"inference_id": inf.inference_id, "cached": inf.cached, "skills": len(inf.skills),
                      "dropped": len(inf.dropped_skills or []), "overall_confidence": inf.overall_confidence}
        except NotEligible as e:
            ok, info = False, {"error": NOT_ELIGIBLE, "retryable": False, "message": e.reason}
        except InferenceFailed as e:
            ok, info = False, {"error": e.code, "retryable": e.retryable, "message": e.message}
        except LookupError as e:
            ok, info = False, {"error": PERMIT_NOT_FOUND, "retryable": False, "message": str(e)}
        except asyncio.TimeoutError:
            ok, info = False, {"error": TIMEOUT, "retryable": True,
                               "message": f"inference over {self.settings['infer_timeout_s']} s"}
        ctx.events.record("step.infer", permit_id=pid, params={"permit_number": number, "attempt": attempt},
                          result={**result, **info}, ok=ok, error_code=info.get("error"),
                          retryable=info.get("retryable"), latency_ms=int((self.clock() - started) * 1000))
        self._observe(ctx, pid, before)
        return ok, {**info, **result}

    # ---- reports ---------------------------------------------------------------------

    def _report(self, ctx: _Run, rows: List[Dict[str, Any]], results: List[Dict[str, Any]],
                usage: Optional[Dict[str, Any]], error: Optional[str], started: float, started_at: datetime,
                params: Dict[str, Any]) -> Dict[str, Any]:
        def count(outcomes) -> int:
            return sum(1 for r in results if r.get("outcome") in outcomes)

        queued = count(("queued", "interrupted"))
        status = "failed" if error else ("stopped" if self.stop_reason and (queued or self.stop_reason in
                                                                             SIGNAL_REASONS) else "completed")
        return {
            "run_id": ctx.run_id, "jurisdiction": ctx.jurisdiction, "status": status,
            "error": error or (self.stop_reason if status == "stopped" else None), "params": params,
            "planned": len(rows), "completed": count(COMPLETED_OUTCOMES), "failed": count(FAILED_OUTCOMES),
            "skipped": count(("skipped",)), "queued": queued,
            "llm_calls": (usage or {}).get("calls", 0), "tokens": (usage or {}).get("tokens", {}),
            "est_cost": (usage or {}).get("est_cost"), "cost_known": (usage or {}).get("cost_known", True),
            "illegal_transitions": ctx.illegal, "started_at": started_at.isoformat() + "Z",
            "duration_ms": int((self.clock() - started) * 1000), "results": results,
        }

    def status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """`pts pipeline status --run`: the run row and each permit's state when the run stopped."""
        run = self.store.get_pipeline_run(run_id)
        if run is None:
            return None
        events = self.store.run_events(run_id)
        done = {e["result_json"]["permit_number"]: e["result_json"] for e in events
                if e["event"] == "permit.done" and e.get("result_json")}
        planned: List[str] = next((e["params_json"].get("permits_planned") or [] for e in events
                                   if e["event"] == "pipeline.start" and e.get("params_json")), [])
        permits = [done.get(n) or {"permit_number": n, "outcome": "queued"} for n in planned]
        transitions = sum(1 for e in events if e["event"] == "transition")
        return {"run": run, "permits": permits, "transitions": transitions,
                "illegal_transitions": sum(1 for e in events if e.get("error_code") == "ILLEGAL_TRANSITION")}

    def cohort_status(self, jurisdiction: str, since: datetime) -> Dict[str, Any]:
        """`pts pipeline status --since`: the dashboard over permits touched since `since`."""
        return {"jurisdiction": jurisdiction, "since": since.isoformat(),
                **self.store.cohort_status(jurisdiction, since)}

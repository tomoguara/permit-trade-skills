"""The per-permit pipeline (F1, F2, F4) and the batch runner.

For one permit:

1. TTL / dedup: skip when the latest snapshot is younger than
   `min_snapshot_age` and was built from the same catalog row
   (`snapshot.source_hash == permits.raw_hash`), unless forced.
2. Try the jurisdiction's strategies in order (e.g. redirect_chase -> soda_row).
   Each strategy gets up to `retries` retries (backoff 1s, 3s; Retry-After
   honoured, capped) inside one per-permit budget, on a per-host rate limit.
3. Validate fetched content with the F2 signals; the first strategy that
   passes wins. A fallback win is `degraded`.
4. soda_row with Socrata unreachable (a retryable failure) uses the permit's
   stored `raw_row` instead: the same source row, marked degraded.
5. Nothing passed but something was fetched, and the judge is enabled: ask the
   LLM judge about the best candidate.
6. Persist: snapshot + DETAIL_READY, or DETAIL_FAILED (only when the permit
   has no usable snapshot yet); one `detail_fetch_results` row either way.

Failures never raise out of `process`/`run`: every permit ends as an
ok / failed / skipped outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from permit_trade_skills.detail import judge as judge_mod
from permit_trade_skills.detail import signals as F2
from permit_trade_skills.detail.config import DetailConfig, load_config
from permit_trade_skills.detail.fetchers import Fetcher, host_of
from permit_trade_skills.detail.models import (
    BUDGET_EXCEEDED,
    HUMAN_GATE,
    HUMAN_GATE_ACTIVE,
    NETWORK,
    NO_DETAIL_URL,
    PORTAL_STRATEGIES,
    SODA_ROW,
    TIMEOUT,
    VALIDATION_FAILED,
    Attempt,
    DetailDocument,
    FetchResult,
    PermitOutcome,
    PermitRef,
    SignalReport,
)
from permit_trade_skills.detail.normalize import Extraction, extract_portal, extract_row, normalize
from permit_trade_skills.detail.ports import DetailStore
from permit_trade_skills.detail.ratelimit import HostRateLimiter

log = logging.getLogger("pts.detail")

INTERNAL = "INTERNAL"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def soql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def soda_row_url(config: DetailConfig, permit_number: str) -> Optional[str]:
    if not config.soda_resource_url:
        return None
    where = quote(f"{config.soda_key_column}={soql_literal(permit_number)}", safe="=',")
    return f"{config.soda_resource_url}?$where={where}&$limit=1"


def snapshot_document(snap: Dict[str, Any], permit_number: str, jurisdiction: str) -> DetailDocument:
    return DetailDocument(
        permit_number=permit_number, jurisdiction=jurisdiction, final_url=snap["final_url"],
        content_text=snap["content_text"], fields=snap.get("fields") or {},
        fetch_strategy=snap["fetch_strategy"], content_hash=snap["content_hash"],
        degraded=bool(snap.get("degraded")), notes=list(snap.get("notes") or []),
        signals=snap.get("signals"), fetched_at=snap.get("fetched_at"), snapshot_id=snap.get("id"),
    )


class _Candidate:
    """Fetched content that failed (or awaits) F2 validation."""

    def __init__(self, strategy: str, fetch: FetchResult, extraction: Extraction, report: SignalReport,
                 row: Optional[Dict[str, Any]], from_store: bool, index: int):
        self.strategy, self.fetch, self.extraction, self.report = strategy, fetch, extraction, report
        self.row, self.from_store, self.index = row, from_store, index


class DetailPipeline:
    def __init__(self, store: DetailStore, fetcher: Fetcher, *, settings: Dict[str, Any],
                 limiter: Optional[HostRateLimiter] = None, judge_provider: Any = None,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.fetcher = fetcher
        self.settings = settings
        self.limiter = limiter or HostRateLimiter(settings["rate_limit_per_minute"])
        self.judge_provider = judge_provider
        self.sleep = sleep
        self.clock = clock
        self.host_gates: Dict[str, float] = {}     # host -> clock() until which it is gated
        self._configs: Dict[str, DetailConfig] = {}

    # ---- configuration -----------------------------------------------------

    def config(self, jurisdiction: str) -> DetailConfig:
        if jurisdiction not in self._configs:
            j = self.store.jurisdiction(jurisdiction)
            if j is None:
                raise LookupError(f"unknown jurisdiction {jurisdiction!r}")
            cfg = load_config(j)
            for host, rate in cfg.rate_limits_per_minute.items():
                self.limiter.set_rate(host, rate)
            self._configs[jurisdiction] = cfg
        return self._configs[jurisdiction]

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    # ---- fetching ----------------------------------------------------------

    async def _fetch(self, url: str, strategy: str, allowed: Sequence[str],
                     deadline: float) -> Tuple[FetchResult, int]:
        retries = int(self.settings["retries"])
        backoff = list(self.settings["backoff_s"]) or [1]
        result = FetchResult(ok=False, final_url=url, error=BUDGET_EXCEEDED, retryable=True)
        tries = 0
        for attempt in range(retries + 1):
            remaining = deadline - self.clock()
            if remaining <= 0:
                return FetchResult(ok=False, final_url=url, error=BUDGET_EXCEEDED, retryable=True,
                                   detail="per-permit budget spent"), tries
            await self.limiter.acquire(host_of(url))
            tries += 1
            try:
                result = await asyncio.wait_for(self.fetcher.fetch(url, strategy, allowed_hosts=allowed),
                                                timeout=max(0.001, deadline - self.clock()))
            except asyncio.TimeoutError:
                result = FetchResult(ok=False, final_url=url, error=TIMEOUT, retryable=True,
                                     detail="per-permit budget exceeded")
            except Exception as e:  # failure isolation: a fetcher bug is a failed attempt
                result = FetchResult(ok=False, final_url=url, error=NETWORK, retryable=True,
                                     detail=f"{type(e).__name__}: {e}")
            if result.ok or not result.retryable or attempt == retries:
                break
            delay = backoff[min(attempt, len(backoff) - 1)]
            if result.retry_after_s is not None:
                delay = min(max(delay, result.retry_after_s), float(self.settings["max_retry_after_s"]))
            if self.clock() + delay >= deadline:
                break
            await self.sleep(delay)
        return result, tries

    def _target(self, strategy: str, permit: PermitRef, config: DetailConfig
                ) -> Tuple[Optional[str], List[str]]:
        if strategy == SODA_ROW:
            url = soda_row_url(config, permit.permit_number)
            return url, [host_of(url)] if url else []
        return permit.detail_url, config.allowed_hosts_for(permit.detail_url)

    @staticmethod
    def _row_from(fetch: FetchResult) -> Optional[Dict[str, Any]]:
        for resp in fetch.responses:
            try:
                rows = json.loads(resp.body)
            except ValueError:
                continue
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows[0]
        return None

    # ---- one permit --------------------------------------------------------

    def _fresh_snapshot(self, permit: PermitRef, max_age: timedelta) -> Optional[Dict[str, Any]]:
        snap = self.store.latest_snapshot(permit.permit_id)
        if snap is None or snap.get("fetched_at") is None:
            return None
        young = _utcnow() - snap["fetched_at"] < max_age
        same_row = bool(permit.raw_hash) and snap.get("source_hash") == permit.raw_hash
        return snap if young and same_row else None

    async def process(self, permit: PermitRef, config: DetailConfig, *, run_id: Optional[str],
                      force: bool = False, max_age: Optional[timedelta] = None) -> PermitOutcome:
        started = self.clock()
        max_age = max_age if max_age is not None else timedelta(hours=self.settings["min_snapshot_age_hours"])
        if not force:
            snap = self._fresh_snapshot(permit, max_age)
            if snap is not None:
                out = PermitOutcome(permit_id=permit.permit_id, permit_number=permit.permit_number,
                                    outcome="skipped", strategy=snap["fetch_strategy"],
                                    final_url=snap["final_url"], content_hash=snap["content_hash"],
                                    snapshot_id=snap["id"], degraded=bool(snap.get("degraded")),
                                    document=snapshot_document(snap, permit.permit_number, permit.jurisdiction))
                return self._record(run_id, out, started)

        deadline = started + float(self.settings["total_budget_s"])
        attempts: List[Attempt] = []
        notes: List[str] = []
        candidates: List[_Candidate] = []
        winner: Optional[_Candidate] = None

        for index, strategy in enumerate(config.strategies):
            url, allowed = self._target(strategy, permit, config)
            if not url:
                code = NO_DETAIL_URL if strategy in PORTAL_STRATEGIES else "NO_SODA_ENDPOINT"
                attempts.append(Attempt(strategy, ok=False, error=code, retryable=False))
                notes.append(f"{strategy}: skipped ({code})")
                continue
            host = host_of(url)
            if strategy in PORTAL_STRATEGIES and self.host_gates.get(host, 0) > self.clock():
                attempts.append(Attempt(strategy, ok=False, url=url, error=HUMAN_GATE_ACTIVE, retryable=True,
                                        detail=f"{host} cooling down after a CAPTCHA"))
                notes.append(f"{strategy}: skipped ({HUMAN_GATE_ACTIVE} for {host})")
                continue

            fetch, tries = await self._fetch(url, strategy, allowed, deadline)
            attempt = Attempt(strategy, ok=fetch.ok, url=url, final_url=fetch.final_url,
                              http_status=fetch.http_status, error=fetch.error,
                              retryable=None if fetch.ok else fetch.retryable, tries=tries, detail=fetch.detail)
            attempts.append(attempt)
            notes.extend(f"{strategy}: {n}" for n in fetch.notes)
            if fetch.blocked_urls:
                log.warning("detail.blocked permit=%s strategy=%s blocked=%s", permit.permit_number,
                            strategy, fetch.blocked_urls)

            row: Optional[Dict[str, Any]] = None
            from_store = False
            if not fetch.ok:
                if fetch.error == HUMAN_GATE:
                    self.host_gates[host] = self.clock() + float(self.settings["human_gate_cooldown_s"])
                if strategy == SODA_ROW and fetch.retryable and permit.raw_row:
                    row, from_store = permit.raw_row, True
                    notes.append(f"soda_row: Socrata unavailable ({fetch.error}); used the stored catalog row")
                else:
                    notes.append(f"{strategy}: failed ({fetch.error}{': ' + fetch.detail if fetch.detail else ''})")
                    continue

            if strategy == SODA_ROW:
                row = row if from_store else self._row_from(fetch)
                if row is None:
                    attempt.ok, attempt.error, attempt.retryable = False, VALIDATION_FAILED, False
                    notes.append("soda_row: response held no row")
                    continue
                extraction = extract_row(row, config)
            else:
                extraction = extract_portal(fetch.html, fetch.responses, config)
            report = F2.score(extraction, permit, config.min_signals)
            attempt.signals = report.count
            cand = _Candidate(strategy, fetch, extraction, report, row, from_store, index)
            if report.passed:
                winner = cand
                break
            attempt.ok, attempt.error, attempt.retryable = False, VALIDATION_FAILED, False
            notes.append(f"{strategy}: validation failed ({report.count}/{config.min_signals} signals, "
                         f"anchor={'yes' if report.anchor else 'no'})")
            candidates.append(cand)

        judged = False
        if winner is None and candidates and self.settings.get("judge_enabled"):
            best = max(candidates, key=lambda c: (c.report.count, -c.index))
            try:
                provider = self.judge_provider or judge_mod.default_provider()
                verdict = judge_mod.judge(provider, permit, best.extraction.text)
                notes.append(f"judge ({verdict.model}): {verdict.verdict}: {verdict.reason}")
                if verdict.relevant:
                    winner, judged = best, True
            except Exception as e:  # judge is advisory; its failure is just a note
                notes.append(f"judge unavailable: {type(e).__name__}: {e}")

        if winner is None:
            return self._fail(permit, run_id, attempts, notes, started)
        return self._succeed(permit, config, run_id, winner, attempts, notes, judged, started)

    def _succeed(self, permit: PermitRef, config: DetailConfig, run_id: Optional[str], win: _Candidate,
                 attempts: List[Attempt], notes: List[str], judged: bool, started: float) -> PermitOutcome:
        final_url = win.fetch.final_url or soda_row_url(config, permit.permit_number) or ""
        if win.strategy == SODA_ROW:
            doc = normalize(permit=permit, config=config, strategy=SODA_ROW, final_url=final_url, row=win.row)
        else:
            doc = normalize(permit=permit, config=config, strategy=win.strategy, final_url=final_url,
                            portal=win.extraction)
        doc.degraded = win.index > 0 or win.from_store or judged
        if judged:
            notes.append("accepted on the judge's verdict (signals below threshold)")
        doc.notes = notes
        doc.signals = win.report.to_dict()
        raw_responses = [asdict(r) for r in win.fetch.responses] or None
        if win.from_store:
            raw_responses = [{"url": "permits.raw_row", "type": "json", "body": json.dumps(win.row)}]
        snapshot_id, inserted, fetched_at = self.store.save_snapshot(
            permit.permit_id, doc.to_dict(), raw_html=win.fetch.html, raw_responses=raw_responses,
            http_status=win.fetch.http_status, source_hash=permit.raw_hash, run_id=run_id)
        doc.snapshot_id, doc.fetched_at = snapshot_id, fetched_at
        self.store.mark_detail_state(permit.permit_id, True)
        attempts[-1].ok = True
        out = PermitOutcome(permit_id=permit.permit_id, permit_number=permit.permit_number, outcome="ok",
                            strategy=win.strategy, final_url=doc.final_url, content_hash=doc.content_hash,
                            snapshot_id=snapshot_id, new_snapshot=inserted, degraded=doc.degraded,
                            attempts=attempts, document=doc, notes=notes)
        return self._record(run_id, out, started)

    def _fail(self, permit: PermitRef, run_id: Optional[str], attempts: List[Attempt], notes: List[str],
              started: float) -> PermitOutcome:
        first = next((a for a in attempts if a.error), None)
        error = first.error if first else INTERNAL
        retryable = any(a.retryable for a in attempts if a.error)
        self.store.mark_detail_state(permit.permit_id, False, error={
            "stage": "detail", "error": error, "retryable": retryable,
            "attempts": [a.to_dict() for a in attempts]})
        out = PermitOutcome(permit_id=permit.permit_id, permit_number=permit.permit_number, outcome="failed",
                            error=error, retryable=retryable, attempts=attempts,
                            final_url=first.final_url if first else None, notes=notes)
        return self._record(run_id, out, started)

    def _record(self, run_id: Optional[str], out: PermitOutcome, started: float) -> PermitOutcome:
        out.duration_ms = int((self.clock() - started) * 1000)
        log.info("detail.result permit=%s outcome=%s strategy=%s error=%s ms=%d", out.permit_number,
                 out.outcome, out.strategy, out.error, out.duration_ms)
        if run_id:
            try:
                self.store.record_result(run_id, {
                    "permit_id": out.permit_id, "permit_number": out.permit_number, "outcome": out.outcome,
                    "fetch_strategy": out.strategy, "final_url": out.final_url,
                    "content_hash": out.content_hash, "snapshot_id": out.snapshot_id,
                    "new_snapshot": out.new_snapshot, "degraded": out.degraded, "error": out.error,
                    "retryable": out.retryable, "attempts": [a.to_dict() for a in out.attempts],
                    "duration_ms": out.duration_ms})
            except Exception:  # the ledger must not take the batch down
                log.exception("detail.ledger_write_failed run=%s permit=%s", run_id, out.permit_number)
        return out

    async def safe_process(self, permit: PermitRef, config: DetailConfig, *, run_id: Optional[str],
                           force: bool = False, max_age: Optional[timedelta] = None) -> PermitOutcome:
        started = self.clock()
        try:
            return await self.process(permit, config, run_id=run_id, force=force, max_age=max_age)
        except Exception as e:
            log.exception("detail.internal_error permit=%s", permit.permit_number)
            out = PermitOutcome(permit_id=permit.permit_id, permit_number=permit.permit_number,
                                outcome="failed", error=INTERNAL, retryable=True,
                                attempts=[Attempt("internal", ok=False, error=INTERNAL,
                                                  detail=f"{type(e).__name__}: {e}")])
            return self._record(run_id, out, started)

    # ---- batch -------------------------------------------------------------

    async def run(self, permits: Sequence[PermitRef], config: DetailConfig, *, run_id: str,
                  trigger: str = "cli", params: Optional[Dict[str, Any]] = None, concurrency: int = 3,
                  force: bool = False, resume: bool = False,
                  max_age: Optional[timedelta] = None) -> Dict[str, Any]:
        """Process a cohort with bounded concurrency; returns the run report."""
        from permit_trade_skills.detail.runs import build_report

        done = self.store.done_permit_ids(run_id) if resume else set()
        self.store.create_run(run_id, config.jurisdiction, trigger, params or {})  # upsert: resume reopens
        limit = max(1, min(int(concurrency), int(self.settings["max_concurrency"])))
        sem = asyncio.Semaphore(limit)

        async def one(p: PermitRef) -> PermitOutcome:
            async with sem:
                return await self.safe_process(p, config, run_id=run_id, force=force, max_age=max_age)

        todo = [p for p in permits if p.permit_id not in done]
        status, error = "completed", None
        try:
            await asyncio.gather(*(one(p) for p in todo))
        except BaseException as e:  # cancelled / interrupted: close the run as failed, then re-raise
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.store.finish_run(run_id, status, error)
            raise
        run = self.store.finish_run(run_id, status, error)
        report = build_report(run, self.store.run_results(run_id))
        report["resumed_skipped"] = len(permits) - len(todo)
        return report

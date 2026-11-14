"""DetailService: the API later stages call (F5).

- `latest(permit_number, jurisdiction)` -> DetailDocument | None: the stored
  snapshot, no network (in-process LRU of the last `detail.cache_size`, then DB).
- `ensure_fresh(permit_number, jurisdiction, max_age)` -> DetailDocument: fetch
  when the snapshot is missing/stale (or the catalog row changed), else return
  the stored one. This is the function the Stage 07 `fetch_detail` tool calls.
  When a refresh fails but an older snapshot exists, that snapshot is returned
  with `degraded=True` and a note; with no snapshot at all, `DetailUnavailable`
  is raised (`error`, `retryable`).
- `fetch_batch(...)`: the cohort runner behind `pts detail fetch`.
- `revalidate(...)`: re-run F2 + normalization on the stored raw content (no network).

`aensure_fresh`/`afetch_batch` are the async forms; the sync forms run them
with asyncio.run and cannot be called from inside a running event loop.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from permit_trade_skills.detail import signals as F2
from permit_trade_skills.detail.config import DetailConfig
from permit_trade_skills.detail.fetchers import DefaultFetcher, Fetcher
from permit_trade_skills.detail.models import SODA_ROW, CapturedResponse, DetailDocument, PermitOutcome
from permit_trade_skills.detail.normalize import extract_portal, extract_row, normalize
from permit_trade_skills.detail.pipeline import DetailPipeline, snapshot_document
from permit_trade_skills.detail.ports import DetailStore, permit_ref
from permit_trade_skills.detail.runs import new_run_id


class DetailUnavailable(Exception):
    def __init__(self, permit_number: str, error: str, retryable: bool, outcome: Optional[PermitOutcome] = None):
        super().__init__(f"detail unavailable for {permit_number}: {error} (retryable={retryable})")
        self.permit_number, self.error, self.retryable, self.outcome = permit_number, error, retryable, outcome


class PermitNotFound(LookupError):
    pass


def _settings() -> Dict[str, Any]:
    from permit_trade_skills.settings import load_settings

    return load_settings()["detail"]


class DetailService:
    def __init__(self, store: DetailStore, fetcher: Optional[Fetcher] = None, *,
                 settings: Optional[Dict[str, Any]] = None, judge_provider: Any = None, **pipeline_kw):
        self.store = store
        self.settings = settings or _settings()
        self._fetcher = fetcher
        self._judge_provider = judge_provider
        self._pipeline_kw = pipeline_kw
        self._cache: "OrderedDict[Tuple[str, str], DetailDocument]" = OrderedDict()

    # ---- plumbing ----------------------------------------------------------

    def pipeline(self) -> DetailPipeline:
        """A pipeline bound to the current event loop (fetchers hold loop-bound clients)."""
        fetcher = self._fetcher or DefaultFetcher(self.settings)
        return DetailPipeline(self.store, fetcher, settings=self.settings,
                              judge_provider=self._judge_provider, **self._pipeline_kw)

    def _permit(self, jurisdiction: str, permit_number: str):
        row = self.store.get_permit(jurisdiction, permit_number)
        if row is None:
            raise PermitNotFound(f"permit {permit_number!r} not found in {jurisdiction!r}")
        return permit_ref(row)

    def _remember(self, key: Tuple[str, str], doc: DetailDocument) -> None:
        self._cache[key] = doc
        self._cache.move_to_end(key)
        while len(self._cache) > int(self.settings.get("cache_size", 512)):
            self._cache.popitem(last=False)

    # ---- reads -------------------------------------------------------------

    def latest(self, permit_number: str, jurisdiction: str) -> Optional[DetailDocument]:
        key = (jurisdiction, permit_number)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        row = self.store.get_permit(jurisdiction, permit_number)
        if row is None:
            return None
        snap = self.store.latest_snapshot(row["id"])
        if snap is None:
            return None
        doc = snapshot_document(snap, permit_number, jurisdiction)
        self._remember(key, doc)
        return doc

    # ---- fetching ----------------------------------------------------------

    async def aensure_fresh(self, permit_number: str, jurisdiction: str,
                            max_age: Optional[timedelta] = None, *, force: bool = False,
                            run_id: Optional[str] = None, trigger: str = "service") -> DetailDocument:
        permit = self._permit(jurisdiction, permit_number)
        pipe = self.pipeline()
        try:
            config = pipe.config(jurisdiction)
            run_id = run_id or new_run_id()
            self.store.create_run(run_id, jurisdiction, trigger,
                                  {"permit": permit_number, "force": force,
                                   "max_age_s": max_age.total_seconds() if max_age else None})
            outcome = await pipe.safe_process(permit, config, run_id=run_id, force=force, max_age=max_age)
            self.store.finish_run(run_id, "completed")
        finally:
            await pipe.aclose()
        if outcome.document is not None:
            self._remember((jurisdiction, permit_number), outcome.document)
            return outcome.document
        snap = self.store.latest_snapshot(permit.permit_id)
        if snap is not None:
            doc = snapshot_document(snap, permit_number, jurisdiction)
            doc.degraded = True
            doc.notes = list(doc.notes) + [f"refresh failed ({outcome.error}); returning the snapshot "
                                           f"fetched {snap['fetched_at']:%Y-%m-%d %H:%M}Z"]
            return doc
        raise DetailUnavailable(permit_number, outcome.error or "UNKNOWN", bool(outcome.retryable), outcome)

    def ensure_fresh(self, permit_number: str, jurisdiction: str, max_age: Optional[timedelta] = None,
                     **kw) -> DetailDocument:
        return asyncio.run(self.aensure_fresh(permit_number, jurisdiction, max_age, **kw))

    def cohort(self, jurisdiction: str, *, permit_numbers: Optional[Sequence[str]] = None,
               status: Optional[str] = None, since: Optional[datetime] = None,
               until: Optional[datetime] = None, limit: int = 25):
        rows = self.store.select_permits(jurisdiction, status=status, since=since, until=until, limit=limit,
                                         permit_numbers=permit_numbers)
        return [permit_ref(r) for r in rows]

    async def afetch_batch(self, jurisdiction: str, permits, *, run_id: Optional[str] = None,
                           params: Optional[Dict[str, Any]] = None, concurrency: Optional[int] = None,
                           force: bool = False, resume: bool = False, trigger: str = "cli") -> Dict[str, Any]:
        pipe = self.pipeline()
        try:
            config = pipe.config(jurisdiction)
            report = await pipe.run(permits, config, run_id=run_id or new_run_id(), trigger=trigger,
                                    params=params, concurrency=concurrency or self.settings["concurrency"],
                                    force=force, resume=resume)
        finally:
            await pipe.aclose()
        self._cache.clear()
        return report

    def fetch_batch(self, jurisdiction: str, permits, **kw) -> Dict[str, Any]:
        return asyncio.run(self.afetch_batch(jurisdiction, permits, **kw))

    def plan(self, jurisdiction: str, permits) -> List[Dict[str, Any]]:
        """--dry-run: what a fetch would do per permit (no network, no writes)."""
        pipe = DetailPipeline(self.store, fetcher=None, settings=self.settings)  # type: ignore[arg-type]
        config = pipe.config(jurisdiction)
        max_age = timedelta(hours=self.settings["min_snapshot_age_hours"])
        out = []
        for p in permits:
            fresh = pipe._fresh_snapshot(p, max_age)
            out.append({"permit_number": p.permit_number, "action": "skip (fresh snapshot)" if fresh else "fetch",
                        "strategies": list(config.strategies), "detail_url": p.detail_url})
        return out

    # ---- offline revalidation ---------------------------------------------

    def revalidate(self, permit_number: str, jurisdiction: str) -> Dict[str, Any]:
        """Re-run extraction + F2 on the stored raw content of the latest snapshot."""
        permit = self._permit(jurisdiction, permit_number)
        snap = self.store.latest_snapshot(permit.permit_id)
        if snap is None:
            raise DetailUnavailable(permit_number, "NO_SNAPSHOT", True)
        pipe = DetailPipeline(self.store, fetcher=None, settings=self.settings)  # type: ignore[arg-type]
        config: DetailConfig = pipe.config(jurisdiction)
        responses = [CapturedResponse(r["url"], r["type"], r["body"]) for r in (snap.get("raw_responses") or [])]
        result: Dict[str, Any] = {"permit_number": permit_number, "snapshot_id": snap["id"],
                                  "fetch_strategy": snap["fetch_strategy"], "stored_hash": snap["content_hash"]}
        if snap["fetch_strategy"] == SODA_ROW:
            rows = [json.loads(r.body) for r in responses]
            row = rows[0][0] if rows and isinstance(rows[0], list) else (rows[0] if rows else None)
            if row is None:
                result.update(revalidated=False, reason="raw content pruned (raw_responses is NULL)")
                return result
            extraction = extract_row(row, config)
            doc = normalize(permit=permit, config=config, strategy=SODA_ROW, final_url=snap["final_url"], row=row)
        else:
            if not snap.get("raw_html") and not responses:
                result.update(revalidated=False, reason="raw content pruned (raw_html is NULL)")
                return result
            extraction = extract_portal(snap.get("raw_html"), responses, config)
            doc = normalize(permit=permit, config=config, strategy=snap["fetch_strategy"],
                            final_url=snap["final_url"], portal=extraction)
        report = F2.score(extraction, permit, config.min_signals)
        result.update(revalidated=True, signals=report.to_dict(), content_hash_now=doc.content_hash,
                      hash_matches=doc.content_hash == snap["content_hash"],
                      missing_citations=F2.missing_citations(snap["content_text"], permit))
        return result

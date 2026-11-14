"""Data lifecycle & retention (`pts db prune`).

Phase 1 policy:
- permits.raw_row: retained forever (audit + eval replay).
- detail_snapshots.raw_html and raw_responses: NULLed after 90 days (content_text,
  fields and signals are never pruned).
- detail_fetch_runs / detail_fetch_results: retained (small; the audit trail).
- skill_inferences: retained forever.
- sync_metadata: only the latest 512 runs per jurisdiction are kept.
- nlu_cache: rows past expires_at (24h TTL) are deleted.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

RAW_HTML_RETENTION_DAYS = 90
SYNC_RUNS_TO_KEEP = 512


@dataclass
class PruneResult:
    raw_html_nulled: int
    sync_runs_deleted: int
    dry_run: bool
    nlu_cache_deleted: int = 0


def prune(session: Session, raw_html_days: int = RAW_HTML_RETENTION_DAYS,
          keep_sync_runs: int = SYNC_RUNS_TO_KEEP, dry_run: bool = False) -> PruneResult:
    """Apply the retention policy in the caller's transaction. Returns affected row counts."""
    html_where = (
        "(raw_html IS NOT NULL OR raw_responses IS NOT NULL) AND fetched_at < (now() AT TIME ZONE 'utc') "
        "- make_interval(days => :days)"
    )
    runs_where = (
        "id IN (SELECT id FROM (SELECT id, row_number() OVER ("
        "PARTITION BY jurisdiction_id ORDER BY started_at DESC, id DESC) AS rn "
        "FROM sync_metadata) ranked WHERE rn > :keep)"
    )
    nlu_where = "expires_at <= (now() AT TIME ZONE 'utc')"
    params = {"days": raw_html_days, "keep": keep_sync_runs}
    if dry_run:
        html = session.execute(text(f"SELECT count(*) FROM detail_snapshots WHERE {html_where}"), params).scalar()
        runs = session.execute(text(f"SELECT count(*) FROM sync_metadata WHERE {runs_where}"), params).scalar()
        nlu = session.execute(text(f"SELECT count(*) FROM nlu_cache WHERE {nlu_where}")).scalar()
        return PruneResult(html, runs, True, nlu)
    html = session.execute(text(f"UPDATE detail_snapshots SET raw_html = NULL, raw_responses = NULL WHERE {html_where}"), params).rowcount
    runs = session.execute(text(f"DELETE FROM sync_metadata WHERE {runs_where}"), params).rowcount
    nlu = session.execute(text(f"DELETE FROM nlu_cache WHERE {nlu_where}")).rowcount
    return PruneResult(html, runs, False, nlu)

"""Main sync engine for permit ingestion."""

import hashlib
import uuid
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from permit_trade_skills.discovery.registry import Registry
from permit_trade_skills.ingest.schemas import SyncMode, SyncReport
from permit_trade_skills.ingest.normalizers import PermitNormalizer, compute_raw_hash
from permit_trade_skills.ingest.progress import Progress
from permit_trade_skills.ingest.soda_client import (
    SchemaViolation, SodaClient, SourceUnavailable, fetch_columns, soda_resource_base,
)
from permit_trade_skills.embeddings import compute_embeddings

# Dropped rows listed individually in the report's warning (the count is exact).
MAX_DROPPED_EXAMPLES = 100
# Incremental Socrata syncs also re-pull rows applied in the last N days (Stage 02 P2);
# a jurisdiction's detail_strategy.refresh_window_days can only widen it.
DEFAULT_WINDOW_DAYS = 90
# Bump when normalization changes what is stored from the same source rows, so an
# identical re-pull is processed again instead of being skipped by its fingerprint.
NORMALIZER_VERSION = "2026-11-09"
# Canonical fields whose source column must exist before a Socrata pull (P3).
CORE_FIELDS = ("permit_num", "status_current", "issue_date")


class SyncEngine:
    """Engine for syncing permits from local file or Socrata to the canonical store."""

    def __init__(self, db_url: str, soda_transport=None):
        """Initialize sync engine with database URL.

        soda_transport: optional httpx transport for the SodaClient (tests).
        """
        self.registry = Registry(database_url=db_url)
        self.sync_reports: List[SyncReport] = []
        self._soda_transport = soda_transport

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def rate_limiter(jurisdiction):
        """Per-host limiter for Socrata calls: settings `ingest.rate_limit_per_minute`,
        overridden per host by detail_strategy.detail.rate_limits_per_minute."""
        from permit_trade_skills.detail.ratelimit import HostRateLimiter
        from permit_trade_skills.settings import load_settings

        per_host = ((jurisdiction.detail_strategy or {}).get("detail") or {}).get("rate_limits_per_minute") or {}
        return HostRateLimiter(load_settings()["ingest"]["rate_limit_per_minute"], per_host)

    def _soda_client(self, base: str, limiter) -> SodaClient:
        return SodaClient(base_url=base, transport=self._soda_transport, rate_limiter=limiter)

    async def count_source_rows(self, jurisdiction_slug: str) -> int:
        """Rows in the jurisdiction's Socrata dataset (for the `--full` estimate)."""
        jurisdiction = self.registry.get(jurisdiction_slug)
        base = soda_resource_base(jurisdiction.base_url, jurisdiction.dataset_id)
        if not base:
            raise ValueError(f"Jurisdiction {jurisdiction.id} has no base_url/dataset_id for a Socrata sync")
        async with self._soda_client(base, self.rate_limiter(jurisdiction)) as client:
            return await client.count()

    @staticmethod
    def incremental_where(cursor: Optional[str], timestamp_field: Optional[str],
                          applied_column: Optional[str], window_days: int,
                          since: Optional[date], today: Optional[date] = None) -> Optional[str]:
        """The SoQL $where of an incremental pull, or None to pull everything.

        - `since`: rows applied on/after it (or, with no applied column, with
          `timestamp_field` on/after it), plus rows past the cursor.
        - cursor: rows past the cursor, plus rows applied in the last `window_days`
          (so recent permits' status changes are re-pulled).
        - neither: None (first run: everything).
        """
        clauses = []
        start = since
        if start is None and cursor:
            start = (today or datetime.utcnow().date()) - timedelta(days=window_days)
        if start is not None:
            column = applied_column or (timestamp_field if since is not None else None)
            if column:
                clauses.append(f"{column} >= '{start.isoformat()}'")
        if cursor and timestamp_field:
            clauses.append(f"{timestamp_field} > '{cursor}'")
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else " OR ".join(f"({c})" for c in clauses)

    @staticmethod
    def fingerprint(rows: List[Dict[str, Any]], permit_column: str, config_version: int) -> str:
        """sha256 over the config version, the normalizer version and each row's
        (permit number, raw hash), order-independent."""
        digest = hashlib.sha256(f"{NORMALIZER_VERSION}\n{config_version}\n".encode())
        for line in sorted(f"{row.get(permit_column)}\t{compute_raw_hash(row)}" for row in rows):
            digest.update(line.encode())
            digest.update(b"\n")
        return digest.hexdigest()

    async def _check_columns(self, jurisdiction, needed: Dict[str, str],
                             warnings: List[Dict[str, Any]], limiter) -> None:
        """P3: fail fast (SchemaViolation) when a needed column left the dataset;
        warn about other mapped columns that are gone (they stay NULL)."""
        columns = set(await fetch_columns(jurisdiction.base_url, jurisdiction.dataset_id,
                                          transport=self._soda_transport, rate_limiter=limiter))
        missing_needed = {field: col for field, col in needed.items() if col not in columns}
        if missing_needed:
            listed = ", ".join(f"{f} -> {c}" for f, c in sorted(missing_needed.items()))
            raise SchemaViolation(
                f"Mapped columns missing from the source dataset: {listed}. "
                f"Update the jurisdiction's field_map / detail_strategy; nothing was written.",
                missing=missing_needed,
            )
        others = {field: col for field, col in jurisdiction.field_map.items()
                  if col not in columns and field not in needed and field != "detail_url"}
        if others:
            warnings.append({
                "type": "missing_column",
                "columns": others,
                "message": f"{len(others)} mapped columns are not in the dataset and stay NULL: "
                           + ", ".join(sorted(others.values())),
            })

    def _failed_report(self, jurisdiction, run_id, start_time, mode, cursor, warnings,
                       error: Exception, dry_run: bool) -> SyncReport:
        report = SyncReport(
            jurisdiction_id=jurisdiction.id,
            jurisdiction_slug=jurisdiction.id,
            run_id=run_id,
            config_version=jurisdiction.config_version,
            pull_count=0, inserted=0, updated=0, unchanged=0, dropped=0,
            warnings=warnings + [{
                "type": "schema_violation" if isinstance(error, SchemaViolation) else "source_unavailable",
                "retryable": error.retryable,
                "status_code": getattr(error, "status_code", None),
                **({"missing": error.missing} if isinstance(error, SchemaViolation) else {}),
                "message": str(error),
            }],
            duration_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000),
            cursor_before=cursor,
            cursor_after=cursor,  # a failed run never moves the cursor
            error=str(error),
            completed_at=datetime.utcnow(),
            mode=mode,
        )
        if not dry_run:
            self._save_report(report)
        return report

    # ---- sync --------------------------------------------------------------

    async def sync(
        self,
        jurisdiction_slug: str,
        full: bool = False,
        dry_run: bool = False,
        limit: int = None,
        local_file: str = None,
        since: Optional[date] = None,
    ) -> SyncReport:
        """
        Sync permits for a jurisdiction.

        Args:
            jurisdiction_slug: Jurisdiction id, or a unique prefix of an active
                jurisdiction's id (resolved to the full id, which is what gets stored)
            full: Pull everything (ignore the cursor and window) and process the
                rows even when they are identical to the last run's
            dry_run: Pull, normalize and compute what would change (the upsert
                runs in a transaction that is rolled back); write nothing
            limit: Maximum number of rows to pull (None for all)
            local_file: Path to local JSON file with permit data (bypasses Socrata)
            since: Socrata only: pull rows applied on/after this date (replaces
                the default window start), plus rows past the cursor

        Returns:
            SyncReport with counts and statistics. A Socrata failure or schema
            drift returns a report with `error` set (saved unless dry_run).

        Raises:
            LookupError: unknown or ambiguous jurisdiction.
            FileNotFoundError: local_file does not exist.
            ValueError: no Socrata base_url/dataset_id, or `since` with `full`/`local_file`.
        """
        run_id = str(uuid.uuid4())
        start_time = datetime.utcnow()
        if since is not None and (full or local_file):
            raise ValueError("--since applies to incremental Socrata syncs only (not --full or --local-file)")

        jurisdiction = self.registry.get(jurisdiction_slug)  # JurisdictionConfig
        if jurisdiction.id != jurisdiction_slug:
            print(f"Resolved jurisdiction '{jurisdiction_slug}' to '{jurisdiction.id}'")

        field_map = jurisdiction.field_map
        detail_strategy = jurisdiction.detail_strategy
        permit_column = field_map.get('permit_num', 'permit_num')
        normalizer = PermitNormalizer(
            jurisdiction_id=jurisdiction.id,
            jurisdiction_slug=jurisdiction.id,
            field_map=field_map,
            status_map=jurisdiction.status_map,
            detail_strategy=detail_strategy,
            default_state=jurisdiction.state,
        )
        extra_warnings: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        timestamp_field: Optional[str] = None

        if local_file:
            local_path = Path(local_file)
            if not local_path.exists():
                raise FileNotFoundError(f"File not found: {local_file}")
            print(f"Loading {local_path} ...")
            with open(local_path, "r") as f:
                all_rows = json.load(f)
            print(f"Loaded {len(all_rows):,} rows")
            if limit:
                all_rows = all_rows[:limit]
            mode = SyncMode.DRY_RUN.value if dry_run else SyncMode.FULL.value
        else:
            base = soda_resource_base(jurisdiction.base_url, jurisdiction.dataset_id)
            if not base:
                raise ValueError(
                    f"Jurisdiction {jurisdiction.id} has no base_url/dataset_id for a Socrata sync"
                )
            timestamp_field = detail_strategy.get('timestamp_field')
            applied_column = field_map.get('applied_at')
            if not full:
                if timestamp_field:
                    cursor = await self._get_cursor(jurisdiction.id)
                else:
                    extra_warnings.append({
                        "type": "no_timestamp_field",
                        "message": "detail_strategy.timestamp_field is not set; pulled everything",
                    })
            window_days = max(DEFAULT_WINDOW_DAYS, int(detail_strategy.get('refresh_window_days') or 0))
            where = None if full else self.incremental_where(
                cursor, timestamp_field, applied_column, window_days, since)
            query = {'$where': where} if where else {}
            if dry_run:
                mode = SyncMode.DRY_RUN.value
            else:
                mode = SyncMode.INCREMENTAL.value if where else SyncMode.FULL.value

            if detail_strategy.get('machine_readable') is False:
                return self._failed_report(
                    jurisdiction, run_id, start_time, mode, cursor, extra_warnings,
                    SourceUnavailable(
                        f"Jurisdiction {jurisdiction.id} is marked machine_readable=false (no API); "
                        f"sync it from an export with --local-file",
                        retryable=False),
                    dry_run)

            # Columns the pull itself depends on must exist (P3).
            needed = {f: field_map[f] for f in CORE_FIELDS if field_map.get(f)}
            if timestamp_field:
                needed.setdefault('timestamp_field', timestamp_field)
            if where and applied_column and applied_column in where:
                needed.setdefault('applied_at', applied_column)

            limiter = self.rate_limiter(jurisdiction)
            try:
                await self._check_columns(jurisdiction, needed, extra_warnings, limiter)
                async with self._soda_client(base, limiter) as client:
                    all_rows = await self._pull_rows(client, query, f"{permit_column} ASC", limit)
            except (SourceUnavailable, SchemaViolation) as e:
                return self._failed_report(jurisdiction, run_id, start_time, mode, cursor,
                                           extra_warnings, e, dry_run)

        pull_count = len(all_rows)
        fingerprint = self.fingerprint(all_rows, permit_column, jurisdiction.config_version)
        max_applied_at = self._max_timestamp(all_rows, normalizer, field_map.get('applied_at'))
        max_synced_at = self._max_timestamp(all_rows, normalizer, ':updated_at')
        cursor_after = None
        if not local_file:
            cursor_after = self._compute_cursor(all_rows, normalizer, timestamp_field) or cursor

        def report(**counts) -> SyncReport:
            return SyncReport(
                jurisdiction_id=jurisdiction.id,
                jurisdiction_slug=jurisdiction.id,
                run_id=run_id,
                config_version=jurisdiction.config_version,
                pull_count=pull_count,
                duration_ms=int((datetime.utcnow() - start_time).total_seconds() * 1000),
                cursor_before=cursor,
                cursor_after=cursor_after,
                error=None,
                completed_at=datetime.utcnow(),
                mode=mode,
                max_applied_at=max_applied_at,
                max_synced_at=max_synced_at,
                fingerprint=fingerprint,
                **counts,
            )

        # Identical to the last successful run (same rows, config and normalizer): nothing to do.
        previous = None if full else self._last_fingerprint(jurisdiction.id)
        if previous and previous[1] == fingerprint and pull_count:
            dropped = sum(1 for row in all_rows if not str(row.get(permit_column) or '').strip())
            print(f"Pulled rows are identical to run {previous[0]}; nothing to do.")
            result = report(inserted=0, updated=0, unchanged=pull_count - dropped, dropped=dropped,
                            status_transitions={}, warnings=extra_warnings + [{
                                "type": "identical_pull", "previous_run_id": previous[0],
                                "message": f"same rows as run {previous[0]}; skipped (use --full to reprocess)",
                            }])
            if not dry_run:
                self._save_report(result)
            return result

        # Normalize; rows without a permit number can't be keyed and are dropped
        # (counted and listed in the report, never silently).
        print(f"Normalizing {len(all_rows):,} rows ...")
        normalized: List[Dict[str, Any]] = []
        dropped_rows: List[Tuple[int, Dict[str, Any]]] = []
        for index, row in enumerate(all_rows):
            permit = normalizer.normalize(row, run_id)
            if not permit.get('permit_number'):
                dropped_rows.append((index, row))
            else:
                normalized.append(permit)
        self._attach_embeddings(normalized, self._stored_descriptions(jurisdiction.id))

        # A dry run performs the same upsert and rolls it back, so its counts are exact.
        inserted, updated, unchanged, transitions = self._upsert_rows(normalized, commit=not dry_run)
        verb = "Would upsert" if dry_run else "Upserted"
        print(f"{verb} rows: inserted={inserted}, updated={updated}, unchanged={unchanged}")

        dropped = len(dropped_rows)
        if pull_count != inserted + updated + unchanged + dropped:
            raise RuntimeError(
                f"sync counts do not reconcile: pulled {pull_count} != inserted {inserted} "
                f"+ updated {updated} + unchanged {unchanged} + dropped {dropped}"
            )

        result = report(inserted=inserted, updated=updated, unchanged=unchanged, dropped=dropped,
                        status_transitions=transitions,
                        warnings=extra_warnings + self._generate_warnings(normalized, dropped_rows))
        if not dry_run:
            self._save_report(result)
        return result

    async def _pull_rows(
        self,
        soda_client: SodaClient,
        query: Dict[str, Any],
        order: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Pull every page (or `limit` rows) in a stable order.

        Errors propagate (SourceUnavailable): a failed pull aborts the run
        before anything is written, so no partial sync is ever recorded.
        """
        all_rows: List[Dict[str, Any]] = []
        page = 1
        while limit is None or len(all_rows) < limit:
            page_size = SodaClient.MAX_LIMIT
            rows = await soda_client.list_rows(query, page=page, limit=page_size, order=order)
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
            if page % 20 == 0:
                print(f"  pulled {len(all_rows):,} rows ...")
        return all_rows[:limit] if limit is not None else all_rows

    EMBED_CHUNK = 1024   # descriptions per embedding call (progress granularity)
    FLUSH_EVERY = 2000   # rows between ORM flushes during the upsert

    def _stored_descriptions(self, jurisdiction_id: str) -> Dict[str, Tuple[Optional[str], bool]]:
        """permit_number -> (stored description, has an embedding) for the jurisdiction."""
        session = self.registry.get_session()
        try:
            rows = session.execute(text(
                "SELECT permit_number, description, description_embedding IS NOT NULL "
                "FROM permits WHERE jurisdiction_id = :j"), {"j": jurisdiction_id}).all()
        finally:
            session.close()
        return {r[0]: (r[1], bool(r[2])) for r in rows}

    def _attach_embeddings(self, normalized: List[Dict[str, Any]],
                           stored: Optional[Dict[str, Tuple[Optional[str], bool]]] = None) -> None:
        """Compute description embeddings in place, only where needed.

        A permit whose stored row already has an embedding of the same
        description is skipped (the upsert keeps the stored embedding). Permits
        without a description, or every permit if the embedding model is
        unavailable, get no ``description_embedding`` key.
        """
        stored = stored or {}
        with_desc = []
        for p in normalized:
            if not p.get("description"):
                continue
            previous = stored.get(p.get("permit_number"))
            if previous and previous[1] and previous[0] == p["description"]:
                continue
            with_desc.append(p)
        skipped = sum(1 for p in normalized if p.get("description")) - len(with_desc)
        if skipped:
            print(f"Reusing stored embeddings for {skipped:,} unchanged descriptions.")
        if not with_desc:
            return
        print(f"Computing embeddings for {len(with_desc):,} descriptions ...")
        progress = Progress("Embedding descriptions", len(with_desc))
        # Encode in chunks so progress is visible; same results as one big call.
        for start in range(0, len(with_desc), self.EMBED_CHUNK):
            chunk = with_desc[start:start + self.EMBED_CHUNK]
            embeddings = compute_embeddings([p["description"] for p in chunk])
            if not embeddings:
                progress.close()
                print("Embedding model unavailable; storing permits without embeddings.")
                return
            for permit, embedding in zip(chunk, embeddings):
                permit["description_embedding"] = embedding
            progress.advance(len(chunk))
        progress.close()

    def _upsert_rows(self, normalized: List[Dict[str, Any]],
                     commit: bool = True) -> tuple[int, int, int, Dict[str, int]]:
        """Upsert normalized rows into permits in ONE transaction (all-or-nothing).

        Returns (inserted, updated, unchanged, status_transitions) where
        status_transitions maps "FROM->TO" to a count. Each status change on an
        existing permit is also appended to status_history. With commit=False
        (dry run) the transaction is rolled back after counting.
        """
        inserted, updated, unchanged = 0, 0, 0
        transitions: Dict[str, int] = {}

        print(f"Upserting {len(normalized):,} rows (single transaction"
              f"{'' if commit else ', rolled back: dry run'}) ...")
        progress = Progress("Upserting permits", len(normalized))
        session = self.registry.get_session()
        try:
            for i, permit in enumerate(normalized, 1):
                # Flush pending writes periodically (still one transaction) so the
                # work happens as the counter moves rather than all at commit.
                if i % self.FLUSH_EVERY == 0:
                    session.flush()
                    session.expunge_all()  # keep the identity map (memory) small
                progress.update(i)
                permit_number = permit.get('permit_number')
                if not permit_number:
                    continue

                existing = self.registry.get_permit_by_number(
                    session, permit['jurisdiction_id'], permit_number
                )

                if existing is None:
                    self._insert_permit(session, permit)
                    inserted += 1
                    continue

                old_status = existing.status
                if self._update_permit(session, existing, permit):
                    updated += 1
                    if existing.status != old_status:
                        self._record_transition(session, existing, old_status, permit)
                        key = f"{old_status}->{existing.status}"
                        transitions[key] = transitions.get(key, 0) + 1
                else:
                    unchanged += 1

            progress.close()
            if commit:
                print("Committing ...")
                session.commit()
            else:
                session.flush()  # surface constraint errors, then discard
                session.rollback()

        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

        return inserted, updated, unchanged, transitions

    def _record_transition(self, session, existing, old_status: str, permit: Dict[str, Any]) -> None:
        """Append a status_history row for a status change on an existing permit."""
        from permit_trade_skills.discovery.registry import StatusHistoryTable

        session.add(StatusHistoryTable(
            permit_id=existing.id,
            jurisdiction_id=existing.jurisdiction_id,
            from_status=old_status,
            to_status=existing.status,
            raw_status=permit.get('status_raw'),
            changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            changed_by='sync',
            run_id=permit['run_id'],
        ))
    
    def _insert_permit(self, session, permit: Dict[str, Any]):
        """Insert a new permit record."""
        from permit_trade_skills.discovery.registry import PermitTable
        
        db_permit = PermitTable(
            jurisdiction_id=permit['jurisdiction_id'],
            jurisdiction_slug=permit['jurisdiction_slug'],
            permit_number=permit['permit_number'],
            permit_class=permit.get('permit_class'),
            permit_class_mapped=permit.get('permit_class_mapped'),
            permit_type_mapped=permit.get('permit_type_mapped'),
            permit_type_desc=permit.get('permit_type_desc'),
            status_raw=permit.get('status_raw'),
            status=permit['status'],
            issue_date=permit.get('issue_date'),
            issued_at=permit.get('issued_at'),
            applied_at=permit.get('applied_at'),
            expires_at=permit.get('expires_at'),
            completed_at=permit.get('completed_at'),
            description=permit.get('description'),
            address=permit.get('address'),
            city=permit.get('city'),
            state=permit.get('state'),
            zip_code=permit.get('zip_code'),
            latitude=permit.get('latitude'),
            longitude=permit.get('longitude'),
            valuation=permit.get('valuation'),
            work_class=permit.get('work_class'),
            building_count=permit.get('building_count'),
            housing_units=permit.get('housing_units'),
            housing_units_added=permit.get('housing_units_added'),
            housing_units_removed=permit.get('housing_units_removed'),
            housing_category=permit.get('housing_category'),
            contractor_name=permit.get('contractor_name'),
            applicant_name=permit.get('applicant_name'),
            status_map_key=permit.get('status_map_key'),
            raw_row=permit.get('raw_row'),
            raw_hash=permit['raw_hash'],
            row_id_source=permit.get('row_id_source'),
            row_version_source=permit.get('row_version_source'),
            warnings=permit.get('warnings') or [],
            source_updated_at=permit.get('source_updated_at'),
            source_synced_at=permit.get('source_synced_at'),
            detail_url=permit.get('detail_url'),
            detail_strategy=permit.get('detail_strategy'),
            detail_fetch_type=permit.get('detail_fetch_type'),
            run_id=permit['run_id'],
            created_at=permit.get('created_at'),
            updated_at=permit.get('updated_at'),
            synced_at=permit.get('synced_at'),
            processing_state=permit.get('processing_state'),
            description_embedding=permit.get('description_embedding'),
        )
        session.add(db_permit)
    
    # Columns that keep their stored value when the incoming value is empty.
    _KEEP_IF_EMPTY = (
        'permit_class', 'permit_class_mapped', 'permit_type_mapped', 'permit_type_desc',
        'status_raw', 'issue_date', 'issued_at', 'description', 'address', 'city',
        'state', 'zip_code', 'latitude', 'longitude', 'valuation', 'work_class',
        'building_count', 'housing_units', 'status_map_key', 'detail_url',
        'detail_strategy', 'detail_fetch_type',
        'applied_at', 'expires_at', 'completed_at', 'housing_units_added',
        'housing_units_removed', 'housing_category', 'contractor_name',
        'applicant_name', 'row_id_source', 'row_version_source',
    )

    def _update_permit(self, session, existing, permit: Dict[str, Any]) -> bool:
        """Apply new data to an existing permit. Returns True if anything changed.

        Unchanged rows are not written at all. Bookkeeping timestamps
        (updated_at, synced_at, source_synced_at) are only touched on change.
        """
        # Only None/"" count as empty: 0 and 0.0 are real values (e.g. zero
        # housing units or project cost) and must overwrite the stored value.
        new_values = {
            field: getattr(existing, field) if permit.get(field) in (None, '') else permit.get(field)
            for field in self._KEEP_IF_EMPTY
        }
        new_values['status'] = permit['status']
        new_values['source_updated_at'] = permit.get('source_updated_at')
        new_values['warnings'] = permit.get('warnings') or []

        changes = {
            field: value for field, value in new_values.items()
            if getattr(existing, field) != value
        }
        if existing.raw_hash != permit['raw_hash']:
            changes['raw_row'] = permit.get('raw_row')
            changes['raw_hash'] = permit['raw_hash']

        # Embeddings are float arrays, so compare presence, not values: take the
        # new one when the description changed or none is stored yet.
        new_embedding = permit.get('description_embedding')
        if new_embedding is not None and (
            'description' in changes or existing.description_embedding is None
        ):
            changes['description_embedding'] = new_embedding

        if not changes:
            return False

        for field, value in changes.items():
            setattr(existing, field, value)
        existing.source_synced_at = permit.get('source_synced_at')
        existing.updated_at = permit.get('updated_at')
        existing.synced_at = permit.get('synced_at')
        existing.run_id = permit['run_id']  # last run that changed this row
        return True

    def _generate_warnings(self, normalized: List[Dict[str, Any]],
                           dropped_rows: List[Tuple[int, Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Run-level warnings for the sync report."""
        warnings: List[Dict[str, Any]] = []

        if dropped_rows:
            examples = [{"row": index, ":id": row.get(':id')}
                        for index, row in dropped_rows[:MAX_DROPPED_EXAMPLES]]
            warnings.append({
                "type": "missing_permit_number",
                "count": len(dropped_rows),
                "rows": examples,
                "truncated": len(dropped_rows) > MAX_DROPPED_EXAMPLES,
                "message": f"{len(dropped_rows)} rows have no permit number and were dropped",
            })

        unknown_statuses = sum(1 for p in normalized if p['status'] == 'UNKNOWN')
        if unknown_statuses > 0:
            warnings.append({
                "type": "unknown_status",
                "count": unknown_statuses,
                "message": f"{unknown_statuses} rows mapped to UNKNOWN status"
            })

        missing_urls = sum(1 for p in normalized if not p.get('detail_url'))
        if missing_urls > 0:
            warnings.append({
                "type": "missing_detail_url",
                "count": missing_urls,
                "message": f"{missing_urls} rows have no detail URL"
            })

        return warnings

    async def _get_cursor(self, jurisdiction_id: str) -> Optional[str]:
        """The high-water mark (cursor_after) of the jurisdiction's latest sync report."""
        session = self.registry.get_session()
        try:
            report = self.registry.get_latest_sync_report(session, jurisdiction_id)
            if report and report.get('cursor_after'):
                return report['cursor_after']
        finally:
            session.close()
        return None

    def _last_fingerprint(self, jurisdiction_id: str) -> Optional[Tuple[str, str]]:
        """(run_id, fingerprint) of the jurisdiction's latest successful, saved run."""
        session = self.registry.get_session()
        try:
            row = session.execute(text(
                "SELECT run_id, fingerprint FROM sync_metadata WHERE jurisdiction_id = :j "
                "AND error IS NULL AND fingerprint IS NOT NULL ORDER BY completed_at DESC LIMIT 1"),
                {"j": jurisdiction_id}).first()
        finally:
            session.close()
        return (row[0], row[1]) if row else None

    @staticmethod
    def _max_timestamp(raw_rows: List[Dict[str, Any]], normalizer: PermitNormalizer,
                       column: Optional[str]) -> Optional[datetime]:
        """Latest parseable value of a source column among the rows (None if unmapped)."""
        if not column:
            return None
        values = (normalizer._parse_timestamp(row, column) for row in raw_rows)
        return max((v for v in values if v is not None), default=None)

    def _compute_cursor(self, raw_rows: List[Dict[str, Any]], normalizer: PermitNormalizer,
                        timestamp_field: Optional[str]) -> Optional[str]:
        """Latest `timestamp_field` value among the pulled rows, as YYYY-MM-DD."""
        if not timestamp_field:
            return None
        latest = None
        for row in raw_rows:
            value = normalizer._parse_timestamp(row, timestamp_field)
            if value and (latest is None or value > latest):
                latest = value
        return latest.strftime('%Y-%m-%d') if latest else None

    def _save_report(self, report: SyncReport):
        """Save sync report to database."""
        session = self.registry.get_session()
        try:
            self.registry.save_sync_report(session, report)
            session.commit()
        finally:
            session.close()

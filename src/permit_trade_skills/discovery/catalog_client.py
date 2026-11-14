"""Catalog client for interacting with catalog.data.gov API."""

import httpx
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
from pathlib import Path

from permit_trade_skills.discovery.schemas import DiscoveryCandidate, DiscoveryStatus, DiscoveryRun
from permit_trade_skills.ingest.soda_client import parse_retry_after

log = logging.getLogger(__name__)


def _extra(dataset: Dict[str, Any], key: str) -> Optional[str]:
    """A value from a CKAN package's `extras` list ([{key, value}]), where data.gov keeps DCAT fields."""
    for item in dataset.get("extras") or []:
        if isinstance(item, dict) and item.get("key") == key:
            return item.get("value")
    return None


def candidate_from_package(dataset: Dict[str, Any]) -> DiscoveryCandidate:
    """A DiscoveryCandidate from a CKAN package (platform unknown until probed)."""
    organization = dataset.get("organization") or {}
    return DiscoveryCandidate(
        id=dataset.get("id", ""),
        title=dataset.get("title", ""),
        organization=organization.get("title", ""),
        url=dataset.get("url") or "",
        platform_type=None,  # set by the verifier
        platform_detected_by="",
        confidence=0.0,
        identifier=_extra(dataset, "identifier"),
        publisher=_extra(dataset, "publisher") or organization.get("title"),
        description=dataset.get("notes"),
        distributions=[
            {"format": r.get("format"), "downloadURL": r.get("url"),
             "describedBy": r.get("describedBy") or r.get("conformsTo")}
            for r in dataset.get("resources") or [] if isinstance(r, dict)
        ],
        metadata=dataset,
        evidence={},
    )


class CatalogClient:
    """Client for catalog.data.gov's CKAN API."""

    BASE_URL = "https://catalog.data.gov/api/3/action"
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_LIMIT = 100
    MAX_RESULTS = 1000
    BACKOFF_BASE = 1.0       # seconds; attempt n waits BACKOFF_BASE * 2**n
    MAX_RETRY_AFTER = 60.0   # cap on a server-requested Retry-After wait
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        base_url: str = None,
        timeout: float = None,
        max_retries: int = 3,
        backoff_base: float = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url or self.BASE_URL
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.max_retries = max_retries  # total attempts
        self.backoff_base = self.BACKOFF_BASE if backoff_base is None else backoff_base
        self._transport = transport  # tests inject httpx.MockTransport
        self._client: Optional[httpx.AsyncClient] = None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": "PermitTradeSkills/1.0"},
            transport=self._transport,
        )

    async def __aenter__(self):
        self._client = self._new_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, endpoint: str, params: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """GET with retries on 429/5xx (honouring Retry-After) and network errors.

        Every attempt is logged with a summary (status, result count, bytes,
        time). Raises the last error when the attempts are used up; other 4xx
        raise httpx.HTTPStatusError at once.
        """
        url = f"{self.base_url}/{endpoint}"
        if self._client is None:
            self._client = self._new_client()

        for attempt in range(self.max_retries):
            last = attempt == self.max_retries - 1
            wait = self.backoff_base * (2 ** attempt)
            started = time.monotonic()
            try:
                response = await self._client.get(url, params=params)
            except httpx.RequestError as e:
                log.warning("catalog GET %s params=%s attempt=%d error=%s",
                            endpoint, params, attempt + 1, e.__class__.__name__)
                if last:
                    raise
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                summary = ""
                if response.is_success:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    result = body.get("result") if isinstance(body, dict) else None
                    if isinstance(result, dict):
                        summary = f" count={result.get('count')} returned={len(result.get('results') or [])}"
                log.info("catalog GET %s params=%s attempt=%d status=%d bytes=%d ms=%d%s",
                         endpoint, params, attempt + 1, response.status_code,
                         len(response.content), elapsed_ms, summary)
                if response.status_code in self.RETRY_STATUSES and not last:
                    requested = parse_retry_after(response, self.MAX_RETRY_AFTER)
                    if requested is not None:
                        wait = requested
                else:
                    response.raise_for_status()
                    return response.json()
            await asyncio.sleep(wait)

        raise RuntimeError("Max retries exceeded")

    async def search_datasets(
        self,
        q: str = "*",
        fq: str = "",
        rows: int = DEFAULT_LIMIT,
        start: int = 0,
        sort: str = "metadata_modified desc",
    ) -> Dict[str, Any]:
        """
        Search datasets on catalog.data.gov.

        Args:
            q: Query string
            fq: Filter query
            rows: Number of results per page
            start: Offset for pagination
            sort: Sort order

        Returns:
            API response with datasets
        """
        params = {
            "q": q,
            "fq": fq,
            "rows": rows,
            "start": start,
            "sort": sort,
        }

        result = await self._request("package_search", params)
        return result

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Get a specific dataset by ID."""
        params = {"id": dataset_id}
        result = await self._request("package_show", params)
        return result

    @staticmethod
    def default_queries(jurisdiction_name: str) -> List[str]:
        """The searches run when no query is given: the jurisdiction name three ways."""
        name = jurisdiction_name.lower()
        return [f"\"{jurisdiction_name}\"", name, f"{name} permits"]

    async def search_by_jurisdiction(
        self,
        jurisdiction_name: str,
        state: str = None,
        limit: int = 100,
        queries: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for datasets related to a jurisdiction.

        Args:
            jurisdiction_name: Name of the city/county
            state: State abbreviation (e.g., 'WA'); not used by the default queries
            limit: Maximum number of results
            queries: Searches to run instead of default_queries(jurisdiction_name)

        Returns:
            List of CKAN package dicts, de-duplicated by id
        """
        queries = queries or self.default_queries(jurisdiction_name)

        all_datasets = []
        seen_ids = set()

        for query in queries:
            if len(all_datasets) >= limit:
                break

            result = await self.search_datasets(
                q=query,
                rows=min(limit - len(all_datasets), self.DEFAULT_LIMIT),
            )

            for dataset in result.get("result", {}).get("results", []):
                dataset_id = dataset.get("id")
                if dataset_id and dataset_id not in seen_ids:
                    seen_ids.add(dataset_id)
                    all_datasets.append(dataset)

                    if len(all_datasets) >= limit:
                        break

        return all_datasets

    async def discovery_run(
        self,
        search_query: str,
        jurisdiction_name: str,
        state: str,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[DiscoveryRun, List[DiscoveryCandidate]]:
        """
        Perform a discovery run for a jurisdiction.

        Args:
            search_query: Label recorded on the run
            jurisdiction_name: Name of the jurisdiction
            state: State abbreviation
            query: A custom catalog search (instead of the default name queries)
            limit: Maximum candidates (default MAX_RESULTS)

        Returns:
            Tuple of (discovery_run, candidates)
        """
        queries = [query] if query else self.default_queries(jurisdiction_name)
        limit = limit or self.MAX_RESULTS
        run = DiscoveryRun(
            search_query=search_query,
            search_parameters={
                "jurisdiction": jurisdiction_name,
                "state": state,
                "queries": queries,
                "limit": limit,
            },
            status=DiscoveryStatus.IN_PROGRESS,
            started_at=datetime.utcnow(),
        )

        try:
            datasets = await self.search_by_jurisdiction(
                jurisdiction_name, state, limit=limit, queries=queries,
            )

            run.total_results = len(datasets)
            run.status = DiscoveryStatus.COMPLETED
            run.completed_at = datetime.utcnow()

            candidates = [candidate_from_package(dataset) for dataset in datasets]
            run.candidates_found = len(candidates)

        except Exception as e:
            log.warning("catalog discovery failed: %s", e)
            run.status = DiscoveryStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            candidates = []

        return run, candidates

    def save_dossier(self, dossier: Any, output_path: Path) -> None:
        """Save discovery dossier to JSON file."""
        import json

        dossier_dict = dossier.model_dump(mode="json") if hasattr(dossier, "model_dump") else dossier
        dossier_dict["created_at"] = datetime.utcnow().isoformat()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(dossier_dict, f, indent=2, default=str)

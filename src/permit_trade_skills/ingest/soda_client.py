"""Socrata SODA 2.1 client for fetching permit data."""

import hashlib
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import httpx


class SourceUnavailable(Exception):
    """The data source could not be read.

    retryable=True: worth trying again later (429, 5xx, network errors after
    the client's own retries). retryable=False: retrying won't help (other 4xx,
    a response that isn't JSON).
    """
    def __init__(self, message: str, retryable: bool = True, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class SchemaViolation(Exception):
    """The source no longer has a column the jurisdiction's config needs (Stage 02 P3).

    Never retryable: the config (field_map / detail_strategy) must change first.
    """
    retryable = False

    def __init__(self, message: str, missing: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.missing = missing or {}  # canonical field -> missing source column


def default_user_agent() -> str:
    """`ingest.user_agent` from config/settings.yaml (or PTS_INGEST_USER_AGENT)."""
    from permit_trade_skills.settings import load_settings

    return load_settings()["ingest"]["user_agent"]


def soda_resource_base(base_url: Optional[str], dataset_id: Optional[str]) -> Optional[str]:
    """Row endpoint base for a Socrata dataset: `<base_url>/resource/<dataset_id>`.

    SodaClient appends `.json`. Returns None when either part is missing.
    (`jurisdictions.api_endpoint` is the `/api/views` catalog root, not rows.)
    """
    base = (base_url or "").rstrip("/")
    if not base or not dataset_id:
        return None
    return f"{base}/resource/{dataset_id}"


def parse_retry_after(response: httpx.Response, cap: float) -> Optional[float]:
    """Seconds requested by a Retry-After header (delta-seconds or HTTP date), clamped to [0, cap]."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), cap)


class SodaClient:
    """Client for Socrata SODA 2.1 API."""

    MAX_LIMIT = 1000
    DEFAULT_TIMEOUT = 60.0
    MAX_RETRIES = 3          # retries after the first attempt
    BACKOFF_BASE = 1.0       # seconds; attempt n waits BACKOFF_BASE * 2**n
    MAX_RETRY_AFTER = 60.0   # cap on a server-requested Retry-After wait
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        base_url: str,
        timeout: float = None,
        max_retries: int = None,
        backoff_base: float = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        user_agent: Optional[str] = None,
        rate_limiter=None,
    ):
        """
        Args:
            user_agent: defaults to settings `ingest.user_agent`.
            rate_limiter: a `detail.ratelimit.HostRateLimiter`; every request
                (including retries) waits for the host's next slot. None: no limit.
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.max_retries = self.MAX_RETRIES if max_retries is None else max_retries
        self.backoff_base = self.BACKOFF_BASE if backoff_base is None else backoff_base
        self._transport = transport  # tests inject httpx.MockTransport
        self.user_agent = user_agent or default_user_agent()
        self.rate_limiter = rate_limiter
        self._client: Optional[httpx.AsyncClient] = None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            transport=self._transport,
        )

    async def __aenter__(self):
        self._client = self._new_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _compute_hash(self, row: Dict[str, Any]) -> str:
        """Compute hash of a raw row for idempotency."""
        row_str = str(row)
        return hashlib.sha256(row_str.encode()).hexdigest()

    def _retry_after(self, response: httpx.Response) -> Optional[float]:
        return parse_retry_after(response, self.MAX_RETRY_AFTER)

    async def _request(self, endpoint: str, params: Dict[str, Any] = None) -> Any:
        """GET with retries; returns parsed JSON or raises SourceUnavailable.

        429 and 5xx are retried (honouring Retry-After), as are network errors.
        Other HTTP errors fail at once with retryable=False.
        """
        url = f"{self.base_url}{endpoint}"
        if self._client is None:
            self._client = self._new_client()

        last_error = ""
        last_status: Optional[int] = None
        for attempt in range(self.max_retries + 1):
            wait = self.backoff_base * (2 ** attempt)
            if self.rate_limiter is not None:
                await self.rate_limiter.acquire(httpx.URL(url).host)
            try:
                response = await self._client.get(url, params=params)
            except httpx.RequestError as e:
                last_error, last_status = f"{e.__class__.__name__}: {e}", None
            else:
                if response.status_code in self.RETRY_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                    last_status = response.status_code
                    requested = self._retry_after(response)
                    if requested is not None:
                        wait = requested
                elif response.is_error:
                    raise SourceUnavailable(
                        f"GET {url} failed: HTTP {response.status_code} {response.text[:200]!r}",
                        retryable=False, status_code=response.status_code,
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as e:
                        raise SourceUnavailable(f"GET {url} returned non-JSON: {e}",
                                                retryable=False, status_code=response.status_code)
            if attempt < self.max_retries:
                await asyncio.sleep(wait)

        raise SourceUnavailable(
            f"GET {url} failed after {self.max_retries + 1} attempts: {last_error}",
            retryable=True, status_code=last_status,
        )

    @staticmethod
    def _rows(result: Any) -> List[Any]:
        """The row list from a SODA response (a list, or an object wrapping `data`/`results`)."""
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get('data') or result.get('results') or []
        return []

    async def list_rows(
        self,
        query: Dict[str, Any],
        page: int = 1,
        limit: int = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List one page of rows.

        Args:
            query: SoQL parameters (e.g. {"$where": "issueddate > '2026-01-01'"})
            page: Page number (1-indexed)
            limit: Rows per page (max 1000)
            order: SoQL $order, e.g. "permitnum ASC". Use a unique column so
                offset paging neither skips nor repeats rows.

        Returns:
            List of row dictionaries
        """
        if limit is None or limit > self.MAX_LIMIT:
            limit = self.MAX_LIMIT

        soql_params: Dict[str, Any] = {}
        for key, value in query.items():
            if key.startswith('$'):
                soql_params[key] = value
            else:
                # A bare condition string is treated as the $where clause.
                soql_params['$where'] = key
        soql_params['$limit'] = limit
        soql_params['$offset'] = (page - 1) * limit
        if order:
            soql_params['$order'] = order

        return self._rows(await self._request('.json', soql_params))

    async def distinct_values(self, column: str, limit: int = 1000) -> List[str]:
        """Distinct non-empty values of a column ($select + $group)."""
        result = await self._request(
            '.json',
            {
                '$select': column,
                '$group': column,
                '$limit': limit,
                '$order': f'{column} ASC',
            }
        )
        values = []
        for row in self._rows(result):
            value = row.get(column) if isinstance(row, dict) else (row[0] if row else None)
            if value not in (None, ''):
                values.append(str(value))
        return values

    async def count(self) -> int:
        """Total row count."""
        result = await self._request('.json', {'$select': 'count(*) AS count'})
        rows = self._rows(result)
        if not rows:
            return 0
        first = rows[0]
        value = first.get('count') if isinstance(first, dict) else first[0]
        return int(float(value or 0))

    async def get_row_by_key(self, key_column: str, key_value: str,
                             select: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get a single row by its key column.

        Args:
            key_column: Column name to search by
            key_value: Value to search for
            select: Optional SoQL $select (e.g. ':*,*' to include system fields such as :id)

        Returns:
            Row dictionary or None if not found
        """
        params: Dict[str, Any] = {
            '$where': f"{key_column}='{key_value.replace(chr(39), chr(39)*2)}'",
            '$limit': 1,
        }
        if select:
            params['$select'] = select
        rows = self._rows(await self._request('.json', params))
        return rows[0] if rows else None

    def normalize_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a raw Socrata row to a consistent format.

        Args:
            row: Raw row dictionary

        Returns:
            Normalized row dictionary
        """
        normalized = {}

        # Map common field patterns
        for key, value in row.items():
            key_lower = key.lower()

            # Normalize field names
            new_key = key_lower

            # Remove any special prefixes
            if new_key.startswith(':'):
                new_key = new_key[1:]

            # Normalize value
            if isinstance(value, str):
                # Clean whitespace
                value = ' '.join(value.split())
                # Handle empty strings
                if value == '' or value.lower() == 'null':
                    value = None

            normalized[new_key] = value

        return normalized


async def fetch_columns(base_url: str, dataset_id: str,
                        transport: Optional[httpx.AsyncBaseTransport] = None,
                        user_agent: Optional[str] = None, rate_limiter=None) -> List[str]:
    """Field names of a Socrata dataset (`<base_url>/api/views/<dataset_id>/columns.json`).

    Raises SourceUnavailable when the endpoint can't be read.
    """
    base = f"{(base_url or '').rstrip('/')}/api/views/{dataset_id}"
    async with SodaClient(base, transport=transport, user_agent=user_agent,
                          rate_limiter=rate_limiter) as client:
        result = await client._request('/columns.json')
    if not isinstance(result, list):
        raise SourceUnavailable(f"{base}/columns.json did not return a column list", retryable=False)
    return [c["fieldName"] for c in result if isinstance(c, dict) and c.get("fieldName")]

"""Fetchers: the only network code in the detail stage.

    class Fetcher(Protocol):
        async def fetch(self, url, strategy, *, allowed_hosts) -> FetchResult

One call is one attempt. Retries, backoff, rate limiting, budgets and the
CAPTCHA host cool-down are the pipeline's job, so they behave identically
with any fetcher.

- `HttpFetcher` (strategies `redirect_chase`, `soda_row`): httpx, redirects
  followed by hand so every hop's host is checked against the allow-list
  *before* it is requested; robots.txt checked per host (portal only); a fresh
  cookie jar per fetch (no credentials, nothing carried between permits).
- `PlaywrightFetcher` (strategy `playwright`): headless Chromium for
  JS-rendered portals, imported lazily (`uv run playwright install chromium`).
  Requests to hosts off the allow-list are aborted; XHR/fetch JSON bodies are
  captured. No page.evaluate, no clicks, no form input.
- `ScriptedFetcher`: canned results for tests (no network, no browser).
- `DefaultFetcher`: dispatches by strategy to the two production fetchers.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, Union
from urllib.parse import urljoin, urlsplit

import httpx

from permit_trade_skills.detail.models import (
    BROWSER_UNAVAILABLE,
    HTTP_ERROR,
    HTTP_RETRYABLE,
    HUMAN_GATE,
    NETWORK,
    NOT_FOUND,
    PLAYWRIGHT,
    REDIRECT_BLOCKED,
    REDIRECT_CHASE,
    ROBOTS_DISALLOWED,
    SODA_ROW,
    TIMEOUT,
    UNSUPPORTED_CONTENT,
    CapturedResponse,
    FetchResult,
)
from permit_trade_skills.detail.normalize import detect_captcha

MAX_REDIRECTS = 10
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class Fetcher(Protocol):
    async def fetch(self, url: str, strategy: str, *,
                    allowed_hosts: Sequence[str]) -> FetchResult: ...

    async def aclose(self) -> None: ...


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_allowed(url: str, allowed_hosts: Sequence[str]) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in {
        h.lower() for h in allowed_hosts}


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _status_failure(resp: httpx.Response, final_url: str, log: List[str]) -> FetchResult:
    retryable = resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500
    return FetchResult(ok=False, final_url=final_url, http_status=resp.status_code,
                       error=HTTP_RETRYABLE if retryable else HTTP_ERROR, retryable=retryable,
                       detail=f"HTTP {resp.status_code}",
                       retry_after_s=parse_retry_after(resp.headers.get("retry-after")),
                       requested_urls=log)


class HttpFetcher:
    """httpx fetcher for `redirect_chase` (portal HTML) and `soda_row` (SODA JSON)."""

    def __init__(self, *, user_agent: str, timeout_s: float = 20.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None, robots=None):
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.transport = transport
        self._robots_client: Optional[httpx.AsyncClient] = None
        self.robots = robots

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=httpx.Timeout(self.timeout_s), follow_redirects=False,
            headers={"User-Agent": self.user_agent,
                     "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})

    def _robots(self):
        if self.robots is None:
            from permit_trade_skills.detail.robots import RobotsCache

            self._robots_client = self._client()
            self.robots = RobotsCache(self._robots_client, self.user_agent)
        return self.robots

    async def aclose(self) -> None:
        if self._robots_client is not None:
            await self._robots_client.aclose()
            self._robots_client = None
            self.robots = None  # ours was bound to the closed client

    async def fetch(self, url: str, strategy: str, *, allowed_hosts: Sequence[str]) -> FetchResult:
        if strategy == SODA_ROW:
            return await self._fetch_soda(url, allowed_hosts)
        if strategy == REDIRECT_CHASE:
            return await self._fetch_portal(url, allowed_hosts)
        return FetchResult(ok=False, error=UNSUPPORTED_CONTENT, detail=f"HttpFetcher cannot run {strategy}")

    async def _get(self, client: httpx.AsyncClient, url: str, log: List[str]
                   ) -> Union[httpx.Response, FetchResult]:
        log.append(url)
        try:
            return await client.get(url)
        except httpx.TimeoutException as e:
            return FetchResult(ok=False, final_url=url, error=TIMEOUT, retryable=True,
                               detail=f"{type(e).__name__}", requested_urls=log)
        except httpx.HTTPError as e:
            return FetchResult(ok=False, final_url=url, error=NETWORK, retryable=True,
                               detail=f"{type(e).__name__}: {e}", requested_urls=log)

    async def _fetch_portal(self, url: str, allowed_hosts: Sequence[str]) -> FetchResult:
        log: List[str] = []
        if not host_allowed(url, allowed_hosts):
            return FetchResult(ok=False, final_url=url, error=REDIRECT_BLOCKED, retryable=False,
                               detail=f"start host {host_of(url)!r} not in allow-list",
                               blocked_urls=[url], requested_urls=log)
        async with self._client() as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                if not await self._robots().allowed(current):
                    return FetchResult(ok=False, final_url=current, error=ROBOTS_DISALLOWED,
                                       retryable=False, detail="disallowed by robots.txt",
                                       requested_urls=log)
                resp = await self._get(client, current, log)
                if isinstance(resp, FetchResult):
                    return resp
                if resp.is_redirect:
                    target = urljoin(current, resp.headers.get("location", ""))
                    if not host_allowed(target, allowed_hosts):
                        return FetchResult(ok=False, final_url=current, http_status=resp.status_code,
                                           error=REDIRECT_BLOCKED, retryable=False,
                                           detail=f"redirect to {host_of(target)!r} blocked",
                                           blocked_urls=[target], requested_urls=log)
                    current = target
                    continue
                if resp.status_code >= 400:
                    return _status_failure(resp, current, log)
                ctype = resp.headers.get("content-type", "")
                body = resp.text
                if detect_captcha(body):
                    return FetchResult(ok=False, final_url=current, http_status=resp.status_code,
                                       html=body, error=HUMAN_GATE, retryable=False,
                                       detail="CAPTCHA / bot challenge detected", requested_urls=log)
                if "json" in ctype:
                    return FetchResult(ok=True, final_url=current, http_status=resp.status_code,
                                       responses=[CapturedResponse(current, "json", body)],
                                       requested_urls=log)
                return FetchResult(ok=True, final_url=current, http_status=resp.status_code,
                                   html=body, requested_urls=log)
            return FetchResult(ok=False, final_url=current, error=HTTP_ERROR, retryable=False,
                               detail="too many redirects", requested_urls=log)

    async def _fetch_soda(self, url: str, allowed_hosts: Sequence[str]) -> FetchResult:
        log: List[str] = []
        if allowed_hosts and not host_allowed(url, allowed_hosts):
            return FetchResult(ok=False, final_url=url, error=REDIRECT_BLOCKED, retryable=False,
                               detail=f"SODA host {host_of(url)!r} not allowed", blocked_urls=[url])
        async with self._client() as client:
            resp = await self._get(client, url, log)
            if isinstance(resp, FetchResult):
                return resp
            if resp.status_code >= 300:
                return _status_failure(resp, url, log)
            try:
                rows = resp.json()
            except ValueError:
                return FetchResult(ok=False, final_url=url, http_status=resp.status_code,
                                   error=UNSUPPORTED_CONTENT, detail="SODA response is not JSON",
                                   requested_urls=log)
            if not isinstance(rows, list) or not rows:
                return FetchResult(ok=False, final_url=url, http_status=resp.status_code,
                                   error=NOT_FOUND, retryable=False, detail="no SODA row for permit",
                                   requested_urls=log)
            return FetchResult(ok=True, final_url=url, http_status=resp.status_code,
                               responses=[CapturedResponse(url, "json", resp.text)], requested_urls=log)


class PlaywrightFetcher:
    """Headless Chromium for JS-rendered portals (lazy import; one shared context)."""

    def __init__(self, *, user_agent: str, nav_timeout_s: float = 20.0, settle_timeout_s: float = 8.0,
                 robots=None):
        self.user_agent = user_agent
        self.nav_timeout_s = nav_timeout_s
        self.settle_timeout_s = settle_timeout_s
        self.robots = robots
        self._pw = None
        self._browser = None
        self._context = None

    async def _ensure(self):
        if self._context is None:
            from playwright.async_api import async_playwright  # lazy: optional dependency

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            self._context = await self._browser.new_context(user_agent=self.user_agent,
                                                            java_script_enabled=True,
                                                            accept_downloads=False)
        return self._context

    async def aclose(self) -> None:
        for obj in (self._context, self._browser):
            if obj is not None:
                await obj.close()
        if self._pw is not None:
            await self._pw.stop()
        self._context = self._browser = self._pw = None

    async def fetch(self, url: str, strategy: str, *, allowed_hosts: Sequence[str]) -> FetchResult:
        if not host_allowed(url, allowed_hosts):
            return FetchResult(ok=False, final_url=url, error=REDIRECT_BLOCKED, retryable=False,
                               detail=f"start host {host_of(url)!r} not in allow-list", blocked_urls=[url])
        if self.robots is not None and not await self.robots.allowed(url):
            return FetchResult(ok=False, final_url=url, error=ROBOTS_DISALLOWED, retryable=False,
                               detail="disallowed by robots.txt")
        try:
            context = await self._ensure()
        except Exception as e:  # ImportError, or chromium not installed
            return FetchResult(ok=False, final_url=url, error=BROWSER_UNAVAILABLE, retryable=False,
                               detail=f"{type(e).__name__}: {e}")
        log: List[str] = []
        blocked: List[str] = []
        captured: List[CapturedResponse] = []
        page = await context.new_page()

        async def gate(route):
            req_url = route.request.url
            if host_allowed(req_url, allowed_hosts):
                log.append(req_url)
                await route.continue_()
            else:
                blocked.append(req_url)
                await route.abort()

        async def on_response(response):
            try:
                if response.request.resource_type in ("xhr", "fetch") and \
                        "json" in (response.headers.get("content-type") or ""):
                    captured.append(CapturedResponse(response.url, "json", await response.text()))
            except Exception:  # body unavailable (redirect, aborted)
                pass

        try:
            await page.route("**/*", gate)
            page.on("response", on_response)
            from playwright.async_api import TimeoutError as PwTimeout

            try:
                main = await page.goto(url, timeout=self.nav_timeout_s * 1000, wait_until="domcontentloaded")
            except PwTimeout:
                return FetchResult(ok=False, final_url=page.url, error=TIMEOUT, retryable=True,
                                   detail="navigation timeout", requested_urls=log, blocked_urls=blocked)
            except Exception as e:
                if blocked:  # the navigation itself was redirected off the allow-list and aborted
                    return FetchResult(ok=False, final_url=url, error=REDIRECT_BLOCKED, retryable=False,
                                       detail="navigation redirected off the allow-list",
                                       requested_urls=log, blocked_urls=blocked)
                return FetchResult(ok=False, final_url=page.url, error=NETWORK, retryable=True,
                                   detail=f"{type(e).__name__}: {e}", requested_urls=log, blocked_urls=blocked)
            try:
                await page.wait_for_load_state("networkidle", timeout=self.settle_timeout_s * 1000)
            except PwTimeout:
                pass  # settle budget spent; take what rendered
            status = main.status if main is not None else None
            final_url = page.url
            if not host_allowed(final_url, allowed_hosts):
                return FetchResult(ok=False, final_url=final_url, error=REDIRECT_BLOCKED, retryable=False,
                                   requested_urls=log, blocked_urls=blocked + [final_url])
            html = await page.content()
            if status is not None and status >= 400:
                retryable = status in RETRYABLE_STATUS or status >= 500
                return FetchResult(ok=False, final_url=final_url, http_status=status,
                                   error=HTTP_RETRYABLE if retryable else HTTP_ERROR, retryable=retryable,
                                   requested_urls=log, blocked_urls=blocked)
            if detect_captcha(html):
                return FetchResult(ok=False, final_url=final_url, http_status=status, html=html,
                                   error=HUMAN_GATE, retryable=False, requested_urls=log, blocked_urls=blocked)
            notes = [f"blocked {len(blocked)} off-allow-list request(s)"] if blocked else []
            return FetchResult(ok=True, final_url=final_url, http_status=status, html=html,
                               responses=captured, requested_urls=log, blocked_urls=blocked, notes=notes)
        finally:
            await page.close()


class DefaultFetcher:
    """Production dispatcher: httpx for redirect_chase/soda_row, Chromium for playwright."""

    def __init__(self, settings: Dict[str, Any], *, transport: Optional[httpx.AsyncBaseTransport] = None):
        ua = settings["user_agent"]
        self.http = HttpFetcher(user_agent=ua, timeout_s=settings["nav_timeout_s"], transport=transport)
        self._settings = settings
        self._playwright: Optional[PlaywrightFetcher] = None

    @property
    def playwright(self) -> PlaywrightFetcher:
        if self._playwright is None:
            self._playwright = PlaywrightFetcher(user_agent=self._settings["user_agent"],
                                                 nav_timeout_s=self._settings["nav_timeout_s"],
                                                 settle_timeout_s=self._settings["settle_timeout_s"])
        return self._playwright

    async def fetch(self, url: str, strategy: str, *, allowed_hosts: Sequence[str]) -> FetchResult:
        if strategy == PLAYWRIGHT:
            if self.playwright.robots is None:
                self.playwright.robots = self.http._robots()
            return await self.playwright.fetch(url, strategy, allowed_hosts=allowed_hosts)
        return await self.http.fetch(url, strategy, allowed_hosts=allowed_hosts)

    async def aclose(self) -> None:
        await self.http.aclose()
        if self._playwright is not None:
            await self._playwright.aclose()


Script = Union[FetchResult, Exception, List[Union[FetchResult, Exception]],
               Callable[[str, str], FetchResult]]


class ScriptedFetcher:
    """Test fetcher: results keyed by (strategy, url), (strategy, '*') or url.

    A list is consumed one item per call (the last item repeats); an Exception
    instance is raised; a callable gets (url, strategy). Every call is recorded
    in `calls` as (strategy, url).
    """

    def __init__(self, script: Optional[Dict[Any, Script]] = None,
                 default: Optional[Script] = None):
        self.script: Dict[Any, Script] = dict(script or {})
        self.default = default
        self.calls: List[Tuple[str, str]] = []
        self._pos: Dict[Any, int] = {}

    def _lookup(self, url: str, strategy: str):
        for key in ((strategy, url), url, (strategy, "*")):
            if key in self.script:
                return key, self.script[key]
        return "default", self.default

    async def fetch(self, url: str, strategy: str, *, allowed_hosts: Sequence[str] = ()) -> FetchResult:
        self.calls.append((strategy, url))
        key, item = self._lookup(url, strategy)
        if isinstance(item, list):
            i = self._pos.get(key, 0)
            self._pos[key] = i + 1
            item = item[min(i, len(item) - 1)]
        if callable(item) and not isinstance(item, FetchResult):
            item = item(url, strategy)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return FetchResult(ok=False, final_url=url, error=NETWORK, retryable=True,
                               detail="ScriptedFetcher: no script for this URL")
        return item

    async def aclose(self) -> None:
        return None


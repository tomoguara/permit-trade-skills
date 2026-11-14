"""robots.txt, fetched once per host and cached for the process.

Rules (RFC 9309): 2xx text -> parse; 4xx, a redirect, or a non-text body (e.g.
Seattle's portal answers /robots.txt with a 302 to its home page) -> no rules,
everything allowed; 5xx or network failure -> treat the host as fully
disallowed for this process (the RFC's "unreachable" rule).
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self._rules: Dict[str, Optional[RobotFileParser]] = {}
        self._disallow_all: Dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def _load(self, origin: str) -> None:
        try:
            resp = await self.client.get(f"{origin}/robots.txt", follow_redirects=False)
        except httpx.HTTPError:
            self._disallow_all[origin] = True
            self._rules[origin] = None
            return
        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 500:
            self._disallow_all[origin] = True
            self._rules[origin] = None
        elif resp.status_code == 200 and ctype.startswith("text/plain"):
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            self._rules[origin] = parser
        else:
            self._rules[origin] = None

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._lock:
            if origin not in self._rules:
                await self._load(origin)
        if self._disallow_all.get(origin):
            return False
        parser = self._rules.get(origin)
        return True if parser is None else parser.can_fetch(self.user_agent, url)


class AllowAllRobots:
    """For tests and fixtures."""

    async def allowed(self, url: str) -> bool:
        return True

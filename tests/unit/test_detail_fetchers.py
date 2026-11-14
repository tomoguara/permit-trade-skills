"""Stage 05 fetchers over httpx.MockTransport (no network, no browser)."""

import asyncio
import json

import httpx

from detail_fakes import FIXTURES, PORTAL_HOST, SODA_URL, fixture
from permit_trade_skills.detail.fetchers import (
    HttpFetcher,
    PlaywrightFetcher,
    ScriptedFetcher,
    parse_retry_after,
)
from permit_trade_skills.detail.models import (
    BROWSER_UNAVAILABLE,
    HTTP_ERROR,
    HTTP_RETRYABLE,
    HUMAN_GATE,
    NOT_FOUND,
    PLAYWRIGHT,
    REDIRECT_BLOCKED,
    REDIRECT_CHASE,
    ROBOTS_DISALLOWED,
    SODA_ROW,
    TIMEOUT,
    FetchResult,
)
from permit_trade_skills.detail.ratelimit import HostRateLimiter
from permit_trade_skills.detail.robots import AllowAllRobots

START = f"https://{PORTAL_HOST}/portal/customize/LinkToRecord.aspx?altId=7083363-CN"
HOP = f"https://{PORTAL_HOST}/Portal/urlrouting.ashx?type=1000&capID1=25SCI"
FINAL = f"https://{PORTAL_HOST}/Portal/cap/CapDetail.aspx?type=1000&capID1=25SCI"


class Recorder:
    """MockTransport handler that records every requested URL."""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404, text="not found")


def _fetcher(routes, robots=None):
    rec = Recorder(routes)
    f = HttpFetcher(user_agent="test", transport=httpx.MockTransport(rec), robots=robots or AllowAllRobots())
    return f, rec


def run(coro):
    return asyncio.run(coro)


def test_redirect_chase_follows_same_host_redirects():
    f, rec = _fetcher({
        START: httpx.Response(302, headers={"location": "/Portal/urlrouting.ashx?type=1000&capID1=25SCI"}),
        HOP: httpx.Response(302, headers={"location": FINAL}),
        FINAL: httpx.Response(200, text=fixture("ssr.html"), headers={"content-type": "text/html"}),
    })
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.ok and res.final_url == FINAL and res.http_status == 200
    assert "7083363-CN" in res.html
    assert rec.requested == [START, HOP, FINAL]


def test_redirect_to_bad_host_is_blocked_before_any_request():
    spec = json.loads(fixture("redirect_bad_host.json"))
    f, rec = _fetcher({spec["start"]: httpx.Response(spec["status"], headers={"location": spec["location"]})})
    res = run(f.fetch(spec["start"], REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert not res.ok and res.error == REDIRECT_BLOCKED and res.retryable is False
    assert res.blocked_urls == [spec["location"]]
    assert not any("evil.example.net" in u for u in rec.requested + res.requested_urls)


def test_start_url_off_allow_list_is_blocked():
    f, rec = _fetcher({})
    res = run(f.fetch("https://evil.example.net/x", REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == REDIRECT_BLOCKED and rec.requested == []


def test_403_is_not_retryable():
    f, _ = _fetcher({START: httpx.Response(403, text=fixture("forbidden_403.html"))})
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == HTTP_ERROR and res.http_status == 403 and res.retryable is False


def test_429_is_retryable_with_retry_after():
    f, _ = _fetcher({START: httpx.Response(429, headers={"retry-after": "7"})})
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == HTTP_RETRYABLE and res.retryable and res.retry_after_s == 7.0


def test_captcha_page_is_a_human_gate():
    f, _ = _fetcher({START: httpx.Response(200, text=fixture("captcha.html"))})
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == HUMAN_GATE and res.retryable is False


def test_timeout_is_retryable():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    f, _ = _fetcher({START: boom})
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == TIMEOUT and res.retryable


def test_robots_disallow_is_respected():
    robots_txt = "User-agent: *\nDisallow: /portal/customize/\n"
    rec = Recorder({
        f"https://{PORTAL_HOST}/robots.txt": httpx.Response(200, text=robots_txt,
                                                            headers={"content-type": "text/plain"}),
    })
    f = HttpFetcher(user_agent="test", transport=httpx.MockTransport(rec))
    res = run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST]))
    assert res.error == ROBOTS_DISALLOWED and res.retryable is False
    assert START not in rec.requested


def test_robots_redirect_means_no_rules():
    rec = Recorder({
        f"https://{PORTAL_HOST}/robots.txt": httpx.Response(302, headers={"location": "/Portal"}),
        START: httpx.Response(200, text=fixture("ssr.html")),
    })
    f = HttpFetcher(user_agent="test", transport=httpx.MockTransport(rec))
    assert run(f.fetch(START, REDIRECT_CHASE, allowed_hosts=[PORTAL_HOST])).ok


def test_soda_row_fetch_and_not_found():
    url = f"{SODA_URL}?$where=permitnum='7083363-CN'&$limit=1"
    f, rec = _fetcher({SODA_URL: httpx.Response(200, text=fixture("soda_row.json"),
                                                headers={"content-type": "application/json"})})
    res = run(f.fetch(url, SODA_ROW, allowed_hosts=["soda.example.gov"]))
    assert res.ok and json.loads(res.responses[0].body)[0]["permitnum"] == "7083363-CN"
    f, _ = _fetcher({SODA_URL: httpx.Response(200, json=[])})
    res = run(f.fetch(url, SODA_ROW, allowed_hosts=["soda.example.gov"]))
    assert res.error == NOT_FOUND and res.retryable is False


def test_playwright_without_a_browser_fails_cleanly(monkeypatch):
    """AC7: no chromium needed; a missing browser is a non-retryable BROWSER_UNAVAILABLE."""
    pw = PlaywrightFetcher(user_agent="test", robots=AllowAllRobots())

    async def no_browser():
        raise ImportError("playwright not installed")

    monkeypatch.setattr(pw, "_ensure", no_browser)
    res = run(pw.fetch(START, PLAYWRIGHT, allowed_hosts=[PORTAL_HOST]))
    assert res.error == BROWSER_UNAVAILABLE and res.retryable is False
    res = run(pw.fetch("https://evil.example.net/", PLAYWRIGHT, allowed_hosts=[PORTAL_HOST]))
    assert res.error == REDIRECT_BLOCKED


def test_scripted_fetcher_sequences_and_records_calls():
    ok = FetchResult(ok=True, final_url="u")
    f = ScriptedFetcher({(REDIRECT_CHASE, "u"): [FetchResult(ok=False, error=TIMEOUT, retryable=True), ok]})
    assert not run(f.fetch("u", REDIRECT_CHASE)).ok
    assert run(f.fetch("u", REDIRECT_CHASE)).ok and run(f.fetch("u", REDIRECT_CHASE)).ok
    assert f.calls == [(REDIRECT_CHASE, "u")] * 3


def test_parse_retry_after():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past
    assert parse_retry_after("soon") is None


def test_rate_limiter_spaces_requests_per_host():
    now = [0.0]
    waits = []

    async def fake_sleep(s):
        waits.append(s)
        now[0] += s

    lim = HostRateLimiter(60, per_host={"slow.example": 5}, clock=lambda: now[0], sleep=fake_sleep)

    async def go():
        for _ in range(3):
            await lim.acquire("slow.example")
        await lim.acquire("fast.example")

    run(go())
    assert waits == [12.0, 12.0]  # 5/min -> 12 s apart; another host is independent
    assert HostRateLimiter(0).rate("x") == 0


def test_fixture_files_exist():
    for name in ("ssr.html", "xhr.html", "xhr.json", "js_bundle.html", "js_mock_responses.json", "soda_row.json",
                 "captcha.html", "forbidden_403.html", "redirect_bad_host.json", "wrong_permit.html"):
        assert (FIXTURES / name).is_file(), name

"""Stage 05 pipeline against the test database with ScriptedFetcher (no network, no browser).

Covers: A -> B fall-through (AC2), soda_row consistency (AC1), CAPTCHA stop,
bad-host block (AC6), retry budget, TTL skip / new snapshot on catalog change
(AC5), batch isolation (AC4), stored-row fallback, judge, resume, revalidate.
"""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from conftest import TEST_DB_URL
from detail_fakes import (
    FINAL_URL,
    TEST_SETTINGS,
    detail_url,
    fixture,
    portal_ok,
    seed_jurisdiction,
    seed_permit,
    soda_ok,
    soda_row,
    update_catalog_row,
)
from permit_trade_skills.detail.fetchers import HttpFetcher, ScriptedFetcher
from permit_trade_skills.detail.models import (
    HTTP_RETRYABLE,
    HUMAN_GATE,
    HUMAN_GATE_ACTIVE,
    NETWORK,
    NOT_FOUND,
    REDIRECT_BLOCKED,
    REDIRECT_CHASE,
    SODA_ROW,
    TIMEOUT,
    VALIDATION_FAILED,
    FetchResult,
)
from permit_trade_skills.detail.normalize import collapse
from permit_trade_skills.detail.pipeline import soda_row_url
from permit_trade_skills.detail.robots import AllowAllRobots
from permit_trade_skills.detail.service import DetailService, DetailUnavailable
from permit_trade_skills.detail.signals import missing_citations
from permit_trade_skills.detail.config import load_config
from permit_trade_skills.llm.fake import FakeProvider
from permit_trade_skills.store.details import SqlDetailStore

from detail_fakes import jurisdiction_dict, permit_ref

pytestmark = pytest.mark.usefixtures("clean_db")


def _soda_url(number="7083363-CN"):
    return soda_row_url(load_config(jurisdiction_dict()), number)


def fail(error, retryable, **kw):
    return FetchResult(ok=False, error=error, retryable=retryable, **kw)


class Clock:
    """Fake monotonic clock advanced by the fake sleep."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def service(fetcher, clock=None, **settings):
    clock = clock or Clock()
    judge = settings.pop("judge", None)
    return DetailService(SqlDetailStore(TEST_DB_URL), fetcher, settings={**TEST_SETTINGS, **settings},
                         sleep=clock.sleep, clock=clock, judge_provider=judge)


def snapshot_count(engine, permit_id=None):
    with engine.connect() as c:
        q = "SELECT count(*) FROM detail_snapshots" + (" WHERE permit_id = :p" if permit_id else "")
        return c.execute(text(q), {"p": permit_id}).scalar()


def state_of(engine, permit_id):
    with engine.connect() as c:
        return c.execute(text("SELECT processing_state, state_error FROM permits WHERE id = :p"),
                         {"p": permit_id}).one()


# ---- single permit ------------------------------------------------------------------

def test_portal_success(clean_db):
    seed_jurisdiction(clean_db)
    pid = seed_permit(clean_db)
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok()})
    doc = service(f).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == REDIRECT_CHASE and not doc.degraded
    assert doc.final_url == FINAL_URL and doc.signals["count"] == 5
    assert "PORTAL ONLY:" in doc.content_text
    assert state_of(clean_db, pid)[0] == "DETAIL_READY"
    with clean_db.connect() as c:
        raw_html, source_hash = c.execute(text("SELECT raw_html, source_hash FROM detail_snapshots")).one()
    assert "7083363-CN" in raw_html and source_hash
    assert f.calls == [(REDIRECT_CHASE, detail_url())]


def test_fall_through_to_soda_on_portal_timeout(clean_db):
    """AC2: portal times out (all retries) -> soda_row wins; fetch_strategy=soda_row, degraded=true."""
    seed_jurisdiction(clean_db)
    pid = seed_permit(clean_db)
    clock = Clock()
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): fail(TIMEOUT, True),
                         (SODA_ROW, _soda_url()): soda_ok()})
    doc = service(f, clock).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == SODA_ROW and doc.degraded is True
    assert any("redirect_chase: failed (TIMEOUT" in n for n in doc.notes)
    assert f.calls.count((REDIRECT_CHASE, detail_url())) == 3        # 1 + 2 retries
    assert clock.sleeps == [1, 3]                                     # backoff 1 s, 3 s
    with clean_db.connect() as c:
        strategy, degraded = c.execute(text("SELECT fetch_strategy, degraded FROM detail_snapshots")).one()
    assert (strategy, degraded) == ("soda_row", True)
    assert state_of(clean_db, pid)[0] == "DETAIL_READY"


def test_soda_row_consistency_with_catalog(clean_db):
    """AC1: a soda_row snapshot contains every catalog-sourced value of permits.raw_row verbatim."""
    seed_jurisdiction(clean_db, plan="soda_row")
    seed_permit(clean_db)
    f = ScriptedFetcher({(SODA_ROW, _soda_url()): soda_ok()})
    doc = service(f).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    with clean_db.connect() as c:
        raw_row = c.execute(text("SELECT raw_row FROM permits")).scalar()
    for key, value in raw_row.items():
        if isinstance(value, str):
            assert collapse(value) in doc.content_text, key
    assert missing_citations(doc.content_text, permit_ref()) == []
    assert not doc.degraded  # soda_row is this plan's primary strategy


def test_wrong_permit_page_falls_back(clean_db):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok(fixture("wrong_permit.html")),
                         (SODA_ROW, _soda_url()): soda_ok()})
    doc = service(f).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == SODA_ROW and doc.degraded
    assert any("validation failed (4/4 signals, anchor=no)" in n for n in doc.notes)


def test_stored_row_used_when_socrata_is_down(clean_db):
    seed_jurisdiction(clean_db, plan="soda_row")
    seed_permit(clean_db)
    f = ScriptedFetcher({(SODA_ROW, _soda_url()): fail(HTTP_RETRYABLE, True, detail="HTTP 503")})
    doc = service(f).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == SODA_ROW and doc.degraded
    assert any("used the stored catalog row" in n for n in doc.notes)
    assert missing_citations(doc.content_text, permit_ref()) == []


def test_bad_host_redirect_is_blocked_and_recorded(clean_db):
    """AC6: REDIRECT_BLOCKED, retryable=False, the bad host never requested, the run recorded."""
    seed_jurisdiction(clean_db, plan="redirect_chase")
    pid = seed_permit(clean_db)
    spec = json.loads(fixture("redirect_bad_host.json"))
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == spec["start"]:
            return httpx.Response(spec["status"], headers={"location": spec["location"]})
        return httpx.Response(200, text="should never be reached")

    fetcher = HttpFetcher(user_agent="test", transport=httpx.MockTransport(handler), robots=AllowAllRobots())
    svc = service(fetcher)
    permits = svc.cohort("seattle-kingcounty-wa", permit_numbers=["7083363-CN"])
    report = svc.fetch_batch("seattle-kingcounty-wa", permits, run_id="run-badhost")
    assert report["failed"] == 1 and report["errors"] == {REDIRECT_BLOCKED: 1}
    result = report["results"][0]
    assert result["error"] == REDIRECT_BLOCKED and result["retryable"] is False
    assert not any("evil.example.net" in u for u in requested)
    assert requested == [spec["start"]]
    state, err = state_of(clean_db, pid)
    assert state == "DETAIL_FAILED" and err["error"] == REDIRECT_BLOCKED and err["retryable"] is False
    assert svc.store.get_run("run-badhost")["status"] == "completed"
    assert snapshot_count(clean_db) == 0


def test_captcha_stops_trying_the_host(clean_db):
    seed_jurisdiction(clean_db, plan="redirect_chase")
    seed_permit(clean_db, soda_row(permitnum="7000001-CN", issueddate="2026-02-01T00:00:00.000"))
    seed_permit(clean_db, soda_row(permitnum="7000002-CN", issueddate="2026-01-01T00:00:00.000"))
    f = ScriptedFetcher({(REDIRECT_CHASE, "*"): fail(HUMAN_GATE, False, html=fixture("captcha.html"))})
    svc = service(f)
    permits = svc.cohort("seattle-kingcounty-wa", status="ISSUED", limit=10)
    report = svc.fetch_batch("seattle-kingcounty-wa", permits, run_id="run-captcha", concurrency=1)
    errors = {r["permit_number"]: (r["error"], r["retryable"]) for r in report["results"]}
    assert errors == {"7000001-CN": (HUMAN_GATE, False), "7000002-CN": (HUMAN_GATE_ACTIVE, True)}
    assert len(f.calls) == 1  # the second permit never touched the gated host


def test_retry_after_is_honoured_and_capped(clean_db):
    seed_jurisdiction(clean_db, plan="redirect_chase")
    seed_permit(clean_db)
    clock = Clock()
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): [
        fail(HTTP_RETRYABLE, True, retry_after_s=120), fail(HTTP_RETRYABLE, True, retry_after_s=5), portal_ok()]})
    doc = service(f, clock).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == REDIRECT_CHASE
    assert clock.sleeps == [30, 5]  # 120 capped at max_retry_after_s; then max(backoff 3, 5)


def test_per_permit_budget_stops_retries(clean_db):
    seed_jurisdiction(clean_db, plan="redirect_chase")
    seed_permit(clean_db)
    clock = Clock()
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): fail(TIMEOUT, True)})
    with pytest.raises(DetailUnavailable) as exc:
        service(f, clock, total_budget_s=3).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert exc.value.error == TIMEOUT and exc.value.retryable
    assert clock.sleeps == [1] and len(f.calls) == 2  # the 3 s backoff would overrun the 3 s budget


# ---- AC5: idempotency / TTL -----------------------------------------------------------

def test_ttl_skip_then_new_snapshot_after_catalog_update(clean_db):
    seed_jurisdiction(clean_db, plan="soda_row")
    pid = seed_permit(clean_db)
    f = ScriptedFetcher({(SODA_ROW, _soda_url()): soda_ok()})
    svc = service(f)
    first = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    second = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert second.content_hash == first.content_hash and second.snapshot_id == first.snapshot_id
    assert snapshot_count(clean_db, pid) == 1 and len(f.calls) == 1   # second call: no fetch

    # the catalog row changes (pts sync): status Issued -> Completed
    changed = soda_row(statuscurrent="Completed")
    update_catalog_row(clean_db, pid, changed)
    f.script[(SODA_ROW, _soda_url())] = soda_ok(changed)
    third = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert third.content_hash != first.content_hash and "STATUS: Completed" in third.content_text
    assert snapshot_count(clean_db, pid) == 2 and len(f.calls) == 2

    # forced re-fetch of identical content refreshes the row, never duplicates it
    svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa", force=True)
    assert snapshot_count(clean_db, pid) == 2


def test_stale_snapshot_is_refetched(clean_db):
    seed_jurisdiction(clean_db, plan="soda_row")
    seed_permit(clean_db)
    f = ScriptedFetcher({(SODA_ROW, _soda_url()): soda_ok()})
    svc = service(f)
    svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa", max_age=timedelta(0))
    assert len(f.calls) == 2


def test_refresh_failure_returns_the_old_snapshot_and_keeps_state(clean_db):
    seed_jurisdiction(clean_db, plan="soda_row")
    pid = seed_permit(clean_db)
    f = ScriptedFetcher({(SODA_ROW, _soda_url()): soda_ok()})
    svc = service(f)
    good = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    f.script[(SODA_ROW, _soda_url())] = fail(NOT_FOUND, False)
    doc = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa", force=True)
    assert doc.content_hash == good.content_hash and doc.degraded
    assert any("refresh failed (NOT_FOUND)" in n for n in doc.notes)
    assert state_of(clean_db, pid)[0] == "DETAIL_READY"


# ---- AC4: batch isolation ---------------------------------------------------------------

def _number(i):
    return f"71000{i:02d}-CN"


def test_batch_isolation_ten_permits(clean_db):
    """10 permits: 1 network-bad, 1 CAPTCHA, 1 wrong-host -> 7 ok, 3 failed, no exception escapes."""
    seed_jurisdiction(clean_db)
    for i in range(10):
        seed_permit(clean_db, soda_row(permitnum=_number(i), issueddate=f"2026-01-{20 - i:02d}T00:00:00.000"))
    script = {}
    for i in range(10):
        n = _number(i)
        script[(REDIRECT_CHASE, detail_url(n))] = portal_ok(fixture("ssr.html").replace("7083363-CN", n))
        script[(SODA_ROW, _soda_url(n))] = soda_ok(soda_row(permitnum=n))
    bad_network, bad_host, captcha = _number(3), _number(6), _number(9)   # captcha is processed last
    script[(REDIRECT_CHASE, detail_url(bad_network))] = ConnectionError("connection reset")
    script[(REDIRECT_CHASE, detail_url(bad_host))] = fail(REDIRECT_BLOCKED, False,
                                                          blocked_urls=["https://evil.example.net/"])
    script[(REDIRECT_CHASE, detail_url(captcha))] = fail(HUMAN_GATE, False)
    for n in (bad_network, bad_host, captcha):
        script[(SODA_ROW, _soda_url(n))] = fail(NOT_FOUND, False)
    f = ScriptedFetcher(script)
    svc = service(f)
    permits = svc.cohort("seattle-kingcounty-wa", status="ISSUED", limit=10)
    assert len(permits) == 10
    report = svc.fetch_batch("seattle-kingcounty-wa", permits, run_id="run-ten", concurrency=1)

    assert (report["ok"], report["failed"], report["skipped"], report["total"]) == (7, 3, 0, 10)
    assert report["status"] == "completed"
    assert report["errors"] == {NETWORK: 1, REDIRECT_BLOCKED: 1, HUMAN_GATE: 1}
    by_number = {r["permit_number"]: r for r in report["results"]}
    assert by_number[bad_network]["retryable"] is True
    assert by_number[bad_host]["retryable"] is False
    assert by_number[captcha]["retryable"] is False
    assert report["by_strategy"] == {"none": {"failed": 3}, "redirect_chase": {"ok": 7}}
    assert snapshot_count(clean_db) == 7
    with clean_db.connect() as c:
        states = dict(c.execute(text("SELECT processing_state, count(*) FROM permits GROUP BY 1")).all())
    assert states == {"DETAIL_READY": 7, "DETAIL_FAILED": 3}
    # the network failure was retried; the non-retryable ones were not
    assert f.calls.count((REDIRECT_CHASE, detail_url(bad_network))) == 3
    assert f.calls.count((REDIRECT_CHASE, detail_url(bad_host))) == 1


def test_batch_concurrency_three_same_counts(clean_db):
    seed_jurisdiction(clean_db)
    for i in range(6):
        seed_permit(clean_db, soda_row(permitnum=_number(i)))
    f = ScriptedFetcher({(REDIRECT_CHASE, "*"): lambda url, s: portal_ok(
        fixture("ssr.html").replace("7083363-CN", url.rsplit("=", 1)[-1]))})
    svc = service(f)
    report = svc.fetch_batch("seattle-kingcounty-wa", svc.cohort("seattle-kingcounty-wa", limit=10),
                             run_id="run-c3", concurrency=3)
    assert report["ok"] == 6 and report["new_snapshots"] == 6


def test_resume_skips_completed_permits(clean_db):
    seed_jurisdiction(clean_db, plan="soda_row")
    for i in range(3):
        seed_permit(clean_db, soda_row(permitnum=_number(i)))
    script = {(SODA_ROW, _soda_url(_number(i))): soda_ok(soda_row(permitnum=_number(i))) for i in range(3)}
    script[(SODA_ROW, _soda_url(_number(1)))] = fail(NOT_FOUND, False)
    f = ScriptedFetcher(script)
    svc = service(f)
    permits = svc.cohort("seattle-kingcounty-wa", limit=10)
    first = svc.fetch_batch("seattle-kingcounty-wa", permits, run_id="run-resume", force=True)
    assert (first["ok"], first["failed"]) == (2, 1)
    f.script[(SODA_ROW, _soda_url(_number(1)))] = soda_ok(soda_row(permitnum=_number(1)))
    f.calls.clear()
    again = svc.fetch_batch("seattle-kingcounty-wa", permits, run_id="run-resume", force=True, resume=True)
    assert f.calls == [(SODA_ROW, _soda_url(_number(1)))]
    assert (again["ok"], again["failed"], again["resumed_skipped"]) == (3, 0, 2)


# ---- judge ----------------------------------------------------------------------------

THIN_PAGE = ("<html><body><div class='rec-left'><h1><span>Record Number:</span> <span>7083363-CN</span></h1>"
             "</div></body></html>")


@pytest.mark.parametrize("verdict,ok", [("relevant", True), ("irrelevant", False)])
def test_judge_is_advisory(clean_db, verdict, ok):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    provider = FakeProvider({"verdict": verdict, "reason": "test"}, model="fake-judge")
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok(THIN_PAGE),
                         (SODA_ROW, _soda_url()): fail(NOT_FOUND, False)})
    svc = service(f, judge_enabled=True, judge=provider)
    if ok:
        doc = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
        assert doc.degraded and any("judge" in n for n in doc.notes)
    else:
        with pytest.raises(DetailUnavailable) as exc:
            svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
        assert exc.value.error == VALIDATION_FAILED and exc.value.retryable is False
    assert len(provider.calls) == 1


def test_judge_not_called_when_disabled_or_signals_pass(clean_db):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    provider = FakeProvider({"verdict": "irrelevant", "reason": "x"})
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok()})
    doc = service(f, judge_enabled=True, judge=provider).ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    assert doc.fetch_strategy == REDIRECT_CHASE and provider.calls == []


# ---- revalidate / latest ---------------------------------------------------------------

def test_revalidate_and_latest_without_network(clean_db):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    f = ScriptedFetcher({(REDIRECT_CHASE, detail_url()): portal_ok()})
    svc = service(f)
    doc = svc.ensure_fresh("7083363-CN", "seattle-kingcounty-wa")
    calls = len(f.calls)
    result = svc.revalidate("7083363-CN", "seattle-kingcounty-wa")
    assert result["revalidated"] and result["hash_matches"] and result["signals"]["passed"]
    assert result["missing_citations"] == []
    fresh = service(ScriptedFetcher()).latest("7083363-CN", "seattle-kingcounty-wa")
    assert fresh.content_hash == doc.content_hash and fresh.snapshot_id == doc.snapshot_id
    assert len(f.calls) == calls
    assert service(ScriptedFetcher()).latest("0000000-XX", "seattle-kingcounty-wa") is None


def test_dry_run_plan_makes_no_calls(clean_db):
    seed_jurisdiction(clean_db)
    seed_permit(clean_db)
    f = ScriptedFetcher()
    svc = service(f)
    plan = svc.plan("seattle-kingcounty-wa", svc.cohort("seattle-kingcounty-wa", limit=5))
    assert plan == [{"permit_number": "7083363-CN", "action": "fetch",
                     "strategies": ["redirect_chase", "soda_row"], "detail_url": detail_url()}]
    assert f.calls == [] and snapshot_count(clean_db) == 0

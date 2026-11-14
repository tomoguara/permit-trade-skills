"""SyncEngine._pull_rows pages to completion (B6: no 100,000-row cap) and honours --limit."""

import asyncio

import pytest

from permit_trade_skills.ingest.soda_client import SourceUnavailable
from permit_trade_skills.ingest.sync import SyncEngine

PAGE = 1000


class FakeClient:
    def __init__(self, total, fail_page=None):
        self.total, self.fail_page, self.pages = total, fail_page, []

    async def list_rows(self, query, page=1, limit=PAGE, order=None):
        self.pages.append((page, order))
        if page == self.fail_page:
            raise SourceUnavailable("boom")
        start = (page - 1) * limit
        return [{"n": i} for i in range(start, min(start + limit, self.total))]


def pull(client, limit=None):
    engine = SyncEngine("postgresql://unused@localhost/none_test")  # no connection is made
    return asyncio.run(engine._pull_rows(client, {}, "permitnum ASC", limit))


def test_pulls_past_100k_rows():
    client = FakeClient(101 * PAGE + 7)
    rows = pull(client)
    assert len(rows) == 101 * PAGE + 7
    assert len(client.pages) == 102 and {o for _, o in client.pages} == {"permitnum ASC"}


def test_limit_stops_early():
    client = FakeClient(10 * PAGE)
    assert len(pull(client, limit=1500)) == 1500
    assert [p for p, _ in client.pages] == [1, 2]


def test_errors_propagate_instead_of_truncating():
    with pytest.raises(SourceUnavailable):
        pull(FakeClient(5 * PAGE, fail_page=3))

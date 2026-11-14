"""PermitQuery validation and keyset cursor encoding (no database)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from permit_trade_skills.store.query import (
    MAX_PAGE_SIZE,
    PermitQuery,
    PermitSort,
    decode_cursor,
    encode_cursor,
)
from permit_trade_skills.store.schema import is_destructive


def test_jurisdiction_required():
    with pytest.raises(ValueError):
        PermitQuery(jurisdiction="")


@pytest.mark.parametrize("size", [0, MAX_PAGE_SIZE + 1])
def test_page_size_bounds(size):
    with pytest.raises(ValueError):
        PermitQuery(jurisdiction="j", page_size=size)


def test_page_and_cursor_are_exclusive():
    with pytest.raises(ValueError):
        PermitQuery(jurisdiction="j", page=2, cursor="abc")


def test_sort_accepts_string():
    assert PermitQuery(jurisdiction="j", sort="cost_desc").sort is PermitSort.COST_DESC
    with pytest.raises(ValueError):
        PermitQuery(jurisdiction="j", sort="random")


@pytest.mark.parametrize("value", [datetime(2026, 7, 20, 13, 5), 1234.5, "7140363-RR", None])
def test_cursor_round_trip(value):
    c = decode_cursor(encode_cursor(PermitSort.ISSUED_AT_DESC, value, 42))
    assert (c.sort, c.value, c.is_null, c.id) == ("issued_at_desc", value, value is None, 42)


def test_bad_cursor_rejected():
    with pytest.raises(ValueError):
        decode_cursor("not-a-cursor")


def _script(doc):
    return SimpleNamespace(module=SimpleNamespace(__doc__=doc))


def test_destructive_marker_detection():
    assert is_destructive(_script("Drop old column.\n\n--destructive\n"))
    assert not is_destructive(_script('Add the line "--destructive" to this docstring if ...'))
    assert not is_destructive(_script(None))

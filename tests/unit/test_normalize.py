"""Stage 05 F3: normalize is pure, deterministic and never invents values."""

import html as htmllib
import json
import random
import re

import pytest

from detail_fakes import FINAL_URL, SODA_URL, config, fixture, permit_ref, soda_row
from permit_trade_skills.detail.models import PLAYWRIGHT, REDIRECT_CHASE, SODA_ROW, CapturedResponse
from permit_trade_skills.detail.normalize import (
    PORTAL_MARKER,
    collapse,
    content_hash,
    detect_captcha,
    extract_portal,
    find_json_record,
    fold,
    normalize,
    pair_lines,
)
from permit_trade_skills.detail.signals import missing_citations


def _ssr_doc(html=None):
    return normalize(permit=permit_ref(), config=config(), strategy=REDIRECT_CHASE, final_url=FINAL_URL,
                     html=html or fixture("ssr.html"))


def _xhr_responses(body=None):
    return [CapturedResponse("https://services.seattle.gov/api/record", "json", body or fixture("xhr.json"))]


def _js_responses():
    return [CapturedResponse(**r) for r in json.loads(fixture("js_mock_responses.json"))]


# ---- AC1: citation substrate, every strategy ------------------------------------

@pytest.mark.parametrize("strategy,kwargs", [
    (SODA_ROW, {"row": soda_row(), "final_url": SODA_URL}),
    (REDIRECT_CHASE, {"html": fixture("ssr.html"), "final_url": FINAL_URL}),
    (REDIRECT_CHASE, {"html": fixture("xhr.html"), "responses": _xhr_responses(), "final_url": FINAL_URL}),
    (PLAYWRIGHT, {"html": fixture("js_bundle.html"), "responses": _js_responses(), "final_url": FINAL_URL}),
])
def test_catalog_values_are_verbatim_substrings(strategy, kwargs):
    permit = permit_ref()
    doc = normalize(permit=permit, config=config(), strategy=strategy, **kwargs)
    assert missing_citations(doc.content_text, permit) == []
    for value in ("7083363-CN", "3010 E CHERRY ST", "2026-01-15",
                  "Construct alterations to the basement of an existing one family dwelling  per plan."):
        assert fold(value) in fold(doc.content_text)


def test_soda_row_content_is_the_row_in_field_order():
    doc = normalize(permit=permit_ref(), config=config(), strategy=SODA_ROW, final_url=SODA_URL, row=soda_row())
    lines = doc.content_text.splitlines()
    assert lines[:4] == ["PERMIT: 7083363-CN", "STATUS: Issued", "ADDRESS: 3010 E CHERRY ST",
                         "ISSUED: 2026-01-15T00:00:00.000"]
    assert PORTAL_MARKER not in lines
    assert lines[-1] == f"SOURCE: {SODA_URL}"
    assert "LINK.URL: https://services.seattle.gov/portal/customize/LinkToRecord.aspx?altId=7083363-CN" in lines
    # every scalar of the row is rendered (whitespace collapsed)
    for key, value in soda_row().items():
        if isinstance(value, str):
            assert collapse(value) in doc.content_text, key
    assert doc.fields["catalog"]["permit_number"] == "7083363-CN"
    assert "portal" not in doc.fields


# ---- portal-only section ---------------------------------------------------------

def test_ssr_portal_only_section():
    doc = _ssr_doc()
    text = doc.content_text
    _, _, portal = text.partition(PORTAL_MARKER + "\n")
    assert portal, "portal facts must follow the PORTAL ONLY marker"
    assert "Project Description: Basement remodel Construct alterations to the basement of an existing " \
           "one family dwelling, per plan." in portal
    assert "Other Type of Sprinkler System: smoke detectors, carbon monoxide detectors" in portal
    assert "Floor Area (square feet): 916" in portal
    # a portal pair repeating the same catalog field and value is not repeated
    assert "Record Status: Issued" not in portal
    assert "Record Number: 7083363-CN" not in portal
    # nav / footer / script / ads are stripped
    for noise in ("Search All Records", "Privacy", "9999999-XX", "Sponsored", "Payments"):
        assert noise not in text
    assert doc.fields["portal"]["status"] == "Issued"
    assert doc.fields["portal"]["valuation"] == "25000"
    assert doc.fields["attachments"] == ["/Portal/FileDownload.aspx?docName=plans-7083363.pdf"]
    assert doc.fields["portal_source"] == "dom"


def test_xhr_json_record_is_preferred_over_dom():
    doc = normalize(permit=permit_ref(), config=config(), strategy=REDIRECT_CHASE, final_url=FINAL_URL,
                    html=fixture("xhr.html"), responses=_xhr_responses())
    assert doc.fields["portal_source"] == "json"
    assert doc.fields["portal"]["trades"] == "Framing; Electrical; Window installation"
    assert doc.fields["portal"]["scope"].startswith("Basement remodel; new bedroom egress window")
    assert doc.fields["portal"]["classification"] == "Single Family/Duplex"
    assert "inspections[0].type: Framing" in doc.content_text
    assert "Loading record" not in doc.content_text


def test_js_rendering_picks_the_record_among_captured_responses():
    resp, record = find_json_record(_js_responses())
    assert resp.url.endswith("/api/records/7083363-CN")
    assert record["permitNumber"] == "7083363-CN"
    assert find_json_record(_js_responses()[:1]) is None  # the config body is not a record


def test_dom_pairing_rules():
    lines = [("Record Number: 7083363-CN:", True), ("Construction Permit", True),
             ("Work Location", True), ("3010 E CHERRY ST", False),
             ("Project Value:", False), ("25000", False), ("SECTION TITLE", False)]
    pairs, loose = pair_lines(lines)
    assert ("Record Number", "7083363-CN") in pairs
    assert ("Work Location", "3010 E CHERRY ST") in pairs
    assert ("Project Value", "25000") in pairs
    assert loose == []  # short section titles are dropped


# ---- determinism / idempotency ----------------------------------------------------

def _shuffle_more_details(html: str, seed: int) -> str:
    blocks = re.findall(r"<div><span>[^<]*: </span></div><div><span>[^<]*</span></div>", html)
    assert len(blocks) >= 6
    shuffled = blocks[:]
    random.Random(seed).shuffle(shuffled)
    out = html
    for original in blocks:
        out = out.replace(original, "\x00", 1)
    for block in shuffled:
        out = out.replace("\x00", block, 1)
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reordered_dom_gives_identical_content(seed):
    base = _ssr_doc()
    shuffled = _ssr_doc(_shuffle_more_details(fixture("ssr.html"), seed))
    assert shuffled.content_text == base.content_text
    assert shuffled.content_hash == base.content_hash == content_hash(base.content_text)


def _reorder_keys(node, rng):
    if isinstance(node, dict):
        items = list(node.items())
        rng.shuffle(items)
        return {k: _reorder_keys(v, rng) for k, v in items}
    if isinstance(node, list):
        return [_reorder_keys(v, rng) for v in node]
    return node


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reordered_json_keys_give_identical_content(seed):
    base = normalize(permit=permit_ref(), config=config(), strategy=REDIRECT_CHASE, final_url=FINAL_URL,
                     html=fixture("xhr.html"), responses=_xhr_responses())
    body = json.dumps(_reorder_keys(json.loads(fixture("xhr.json")), random.Random(seed)))
    again = normalize(permit=permit_ref(), config=config(), strategy=REDIRECT_CHASE, final_url=FINAL_URL,
                      html=fixture("xhr.html"), responses=_xhr_responses(body))
    assert again.content_text == base.content_text and again.content_hash == base.content_hash
    row = soda_row()
    reordered_row = dict(reversed(list(row.items())))
    a = normalize(permit=permit_ref(), config=config(), strategy=SODA_ROW, final_url=SODA_URL, row=row)
    b = normalize(permit=permit_ref(), config=config(), strategy=SODA_ROW, final_url=SODA_URL, row=reordered_row)
    assert a.content_hash == b.content_hash


def test_hash_changes_when_the_record_changes():
    a = normalize(permit=permit_ref(), config=config(), strategy=SODA_ROW, final_url=SODA_URL, row=soda_row())
    b = normalize(permit=permit_ref(), config=config(), strategy=SODA_ROW, final_url=SODA_URL,
                  row=soda_row(statuscurrent="Completed"))
    assert a.content_hash != b.content_hash


# ---- AC3: zero hallucination surface ------------------------------------------------

def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


@pytest.mark.parametrize("html_name,responses", [
    ("ssr.html", None), ("xhr.html", "xhr"), ("js_bundle.html", "js")])
def test_every_portal_field_value_is_in_the_fixture(html_name, responses):
    captured = {"xhr": _xhr_responses, "js": _js_responses}[responses]() if responses else []
    page = htmllib.unescape(fixture(html_name))
    # the fixture's strings: raw markup, and its visible text (tags -> whitespace)
    source = fold(page + " " + re.sub(r"<[^>]+>", " ", page) + " ".join(r.body for r in captured))
    # JSON bodies hold escaped quotes; compare against the decoded strings too
    for r in captured:
        source += " " + fold(json.dumps(json.loads(r.body), ensure_ascii=False))
    ext = extract_portal(fixture(html_name), captured, config())
    doc = normalize(permit=permit_ref(), config=config(), strategy=REDIRECT_CHASE, final_url=FINAL_URL,
                    portal=ext)
    portal_values = list(_strings(doc.fields["portal"])) + [v for _, v in doc.fields["portal_pairs"]] \
        + list(doc.fields["attachments"])
    assert portal_values
    for value in portal_values:
        pieces = value.split("; ") if ext.source == "json" else [value]  # list items are joined with '; '
        for piece in pieces:
            assert fold(piece) in source, f"invented value: {piece!r}"


def test_captcha_detection():
    assert detect_captcha(fixture("captcha.html"))
    assert not detect_captcha(fixture("ssr.html"))

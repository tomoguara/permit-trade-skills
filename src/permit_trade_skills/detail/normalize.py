"""Pure, network-free normalization: portal HTML / XHR JSON / SODA row -> DetailDocument.

Determinism: the same inputs always give the same `content_text` (and hash).
Portal label/value pairs and JSON keys are sorted, so a reordered DOM or a
JSON body with reordered keys renders identically.

`content_text` layout (the citation substrate for Stage 06):

    PERMIT: 7083363-CN                 <- catalog section: the SODA row, verbatim
    STATUS: Issued                        (field_order first, then the remaining
    ...                                    columns A-Z), whitespace collapsed
    PORTAL ONLY:                       <- portal strategies only: every portal
    Project Description: Basement ...     label/value pair (sorted), except those
    Total Issued Value: 25000              repeating a catalog field's same value
    SOURCE: https://...                <- final URL

Every value is copied from its source (only whitespace is collapsed), so
`normalize` never invents text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from permit_trade_skills.detail.config import DetailConfig
from permit_trade_skills.detail.models import (
    SODA_ROW,
    CapturedResponse,
    DetailDocument,
    PermitRef,
)

PORTAL_MARKER = "PORTAL ONLY:"
_WS = re.compile(r"\s+")
_PUNCT_ONLY = re.compile(r"^[\W_]+$")

SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "template", "svg", "head",
             "select", "option", "button", "iframe", "textarea", "title", "meta", "link"}
AD_TOKENS = {"ad", "ads", "advert", "advertisement", "banner-ad", "cookie-banner", "skip-link"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
             "source", "track", "wbr"}
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
              "figcaption", "figure", "form", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li",
              "main", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
              "tr", "ul", "br", "label", "caption"}
MIN_LOOSE_WORDS = 5
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_RE = re.compile(r"\.(pdf|dwg|dxf|tif|tiff|png|jpe?g|docx?|xlsx?)(\?|$)|attachment|document",
                           re.I)

CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api", "hcaptcha", "h-captcha", "cf-challenge",
                   "challenge-platform", "cf_chl_", "are you a robot", "verify you are human",
                   "captcha")


def collapse(value: Any) -> str:
    """str(value) with whitespace runs collapsed to one space and trimmed."""
    return _WS.sub(" ", str(value)).strip()


def fold(value: Any) -> str:
    """Comparison form: collapsed and case-folded."""
    return collapse(value).casefold()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_captcha(html: Optional[str]) -> bool:
    low = (html or "").lower()
    return any(marker in low for marker in CAPTCHA_MARKERS)


# --------------------------------------------------------------------------- DOM


def _matches(selector: str, tag: str, attrs: Mapping[str, str]) -> bool:
    if selector.startswith("#"):
        return attrs.get("id") == selector[1:]
    if selector.startswith("."):
        return selector[1:] in (attrs.get("class") or "").split()
    return tag == selector.lower()


class _TextExtractor(HTMLParser):
    """Collects text lines (with heading flags) and link hrefs from the selected regions."""

    def __init__(self, include: Sequence[str]):
        super().__init__(convert_charrefs=True)
        self.include = list(include)
        self.stack: List[Tuple[str, bool, bool]] = []   # (tag, skipping, capturing)
        self.lines: List[Tuple[str, bool]] = []
        self.links: List[str] = []
        self._buf: List[str] = []
        self._buf_heading = False

    @property
    def _skipping(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    @property
    def _capturing(self) -> bool:
        if not self.include:
            return True
        return bool(self.stack) and self.stack[-1][2]

    def _flush(self) -> None:
        text = collapse("".join(self._buf))
        if text and not _PUNCT_ONLY.match(text):
            self.lines.append((text, self._buf_heading))
        self._buf = []
        self._buf_heading = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        tokens = set((a.get("class") or "").lower().split()) | {a.get("id", "").lower()}
        skipping = self._skipping or tag in SKIP_TAGS or bool(tokens & AD_TOKENS) \
            or a.get("aria-hidden") == "true" and tag in ("div", "span", "section")
        capturing = self._capturing or any(_matches(s, tag, a) for s in self.include)
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "a" and capturing and not skipping and a.get("href"):
            self.links.append(a["href"])
        if tag in VOID_TAGS:
            return
        self.stack.append((tag, skipping, capturing))
        if tag in HEADINGS and capturing and not skipping:
            self._buf_heading = True

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            if tag in BLOCK_TAGS:
                self._flush()
            return
        if not any(t == tag for t, _, _ in self.stack):
            return  # stray end tag
        if tag in BLOCK_TAGS:
            self._flush()
        while self.stack:
            t, _, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data):
        if self._skipping or not self._capturing:
            return
        if self._buf_heading is False and self.stack and self.stack[-1][0] in HEADINGS:
            self._buf_heading = True
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def dom_lines(html: str, include: Sequence[str] = ()) -> Tuple[List[Tuple[str, bool]], List[str]]:
    """Text lines `(text, is_heading)` and raw hrefs from the regions in `include`
    (CSS-like `#id`, `.class` or a tag name); the whole document when none match."""
    parser = _TextExtractor(include)
    parser.feed(html)
    parser.close()
    if include and not parser.lines:
        parser = _TextExtractor(())
        parser.feed(html)
        parser.close()
    return parser.lines, parser.links


def _clean_value(value: str) -> str:
    return collapse(value).rstrip(":").strip()


def pair_lines(lines: Sequence[Tuple[str, bool]]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Turn text lines into (label, value) pairs; returns (pairs, unpaired lines).

    - "Label:" followed by a value line -> pair
    - "Label: value" on one line (label <= 60 chars) -> pair
    - a heading without a colon opens a block; following plain lines are its value
    """
    pairs: List[Tuple[str, str]] = []
    loose: List[str] = []
    pending: Optional[str] = None
    block: Optional[str] = None
    block_values: List[str] = []

    def close_block():
        nonlocal block, block_values
        if block and block_values:
            pairs.append((block, " ".join(block_values)))
        block, block_values = None, []

    def inline(text: str) -> Optional[Tuple[str, str]]:
        label, _, value = text.partition(": ")
        if 0 < len(label) <= 60 and _clean_value(value):
            return label.strip(), _clean_value(value)
        return None

    for text, heading in lines:
        if pending and heading:
            # A heading is never a label's value: "Record Number: 7083363-CN:" was inline.
            split = inline(pending)
            if split:
                pairs.append(split)
            pending = None
        if text.endswith(":") and len(text) <= 160:
            close_block()
            pending = _clean_value(text[:-1])
            continue
        if pending:
            value = _clean_value(text)
            if value:
                pairs.append((pending, value))
            pending = None
            continue
        if ": " in text and inline(text):
            close_block()
            pairs.append(inline(text))
            continue
        if heading:
            close_block()
            block = text
            continue
        if block:
            block_values.append(text)
        elif len(text.split()) >= MIN_LOOSE_WORDS:
            loose.append(text)  # free prose; short lines are section titles / menu items
    close_block()
    if pending:
        split = inline(pending)
        if split:
            pairs.append(split)
    return pairs, loose


# -------------------------------------------------------------------------- JSON

# Canonical record keys used to pick "the record" among captured JSON bodies.
JSON_KEY_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "permit_number": ("permitnumber", "permitnum", "recordnumber", "recordid", "casenumber",
                      "altid", "applicationnumber", "permitno"),
    "status": ("status", "recordstatus", "permitstatus", "statuscurrent"),
    "issued_date": ("issueddate", "issuedate", "dateissued", "permitissueddate"),
    "description": ("description", "projectdescription", "workdescription", "descriptionofwork"),
    "address": ("address", "worklocation", "siteaddress", "originaladdress1", "location"),
    "permit_type": ("permittype", "recordtype", "type", "permittypedesc", "permittypemapped"),
    "scope": ("scope", "scopeofwork"),
    "classification": ("classification", "workclass", "permitclass"),
    "inspections": ("inspection", "inspections"),
    "trades": ("trade", "trades", "subpermits"),
    "valuation": ("valuation", "jobvalue", "estprojectcost", "totalissuedvalue", "projectvalue"),
    "applied_date": ("applieddate", "applicationdate"),
}
_KEY_INDEX = {syn: canon for canon, syns in JSON_KEY_SYNONYMS.items() for syn in syns}
RECORD_KEYS = ("permit_number", "status", "issued_date", "description", "address", "permit_type",
               "scope", "inspections")
MIN_RECORD_KEYS = 6


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _canonical_key(key: str) -> Optional[str]:
    return _KEY_INDEX.get(_norm_key(key.rsplit(".", 1)[-1]))


def _walk_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def _scalar(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return collapse(value)


def flatten(record: Mapping[str, Any], prefix: str = "") -> List[Tuple[str, str]]:
    """(dotted key, value) for every scalar in `record`; lists of scalars joined with '; '."""
    out: List[Tuple[str, str]] = []
    for key in sorted(record, key=lambda k: str(k)):
        value = record[key]
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(flatten(value, name + "."))
        elif isinstance(value, list):
            scalars = [_scalar(v) for v in value if not isinstance(v, (dict, list))]
            scalars = [s for s in scalars if s]
            if scalars:
                out.append((name, "; ".join(scalars)))
            for i, item in enumerate(v for v in value if isinstance(v, dict)):
                out.extend(flatten(item, f"{name}[{i}]."))
        else:
            s = _scalar(value)
            if s:
                out.append((name, s))
    return out


def record_score(record: Mapping[str, Any]) -> int:
    keys = {_canonical_key(str(k)) for k in record}
    if "classification" in keys:
        keys.add("scope")  # "classification/scope" is one record key
    return sum(1 for k in RECORD_KEYS if k in keys)


def find_json_record(responses: Sequence[CapturedResponse]) -> Optional[Tuple[CapturedResponse, Dict[str, Any]]]:
    """The captured JSON object covering >= 6 canonical record keys (best score wins;
    ties go to the response URL that sorts first)."""
    best: Optional[Tuple[int, str, CapturedResponse, Dict[str, Any]]] = None
    for resp in sorted(responses, key=lambda r: r.url):
        if resp.type != "json":
            continue
        try:
            body = json.loads(resp.body)
        except (ValueError, TypeError):
            continue
        for d in _walk_dicts(body):
            score = record_score(d)
            if score >= MIN_RECORD_KEYS and (best is None or score > best[0]):
                best = (score, resp.url, resp, d)
    return (best[2], best[3]) if best else None


# ------------------------------------------------------------------ extraction


@dataclass
class Extraction:
    """Candidate content from one source, before rendering."""

    source: str                                 # "dom" | "json" | "row"
    pairs: List[Tuple[str, str]] = field(default_factory=list)
    mapped: Dict[str, str] = field(default_factory=dict)
    loose: List[str] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)
    record_url: Optional[str] = None
    pair_keys: List[Optional[str]] = field(default_factory=list)   # fields{} key per pair (or None)

    @property
    def text(self) -> str:
        return "\n".join([f"{k}: {v}" for k, v in self.pairs] + self.loose)


def _map_pairs(pairs: Sequence[Tuple[str, str]], keyfn) -> Tuple[Dict[str, str], List[Optional[str]]]:
    mapped: Dict[str, str] = {}
    keys: List[Optional[str]] = []
    for label, value in pairs:
        key = keyfn(label)
        keys.append(key)
        if key and key not in mapped:
            mapped[key] = value
    return mapped, keys


def extract_portal(html: Optional[str], responses: Sequence[CapturedResponse],
                   config: DetailConfig) -> Extraction:
    """Prefer a structured JSON record among the captured responses; else the DOM."""
    found = find_json_record(responses)
    if found:
        resp, record = found
        pairs = sorted(flatten(record), key=lambda p: (p[0].casefold(), p[1]))
        mapped, keys = _map_pairs(pairs, lambda k: _canonical_key(k) or config.field_key(k))
        return Extraction(source="json", pairs=pairs, mapped=mapped, pair_keys=keys, record_url=resp.url)
    lines, links = dom_lines(html or "", config.portal_include)
    pairs, loose = pair_lines(lines)
    pairs = sorted(pairs, key=lambda p: (p[0].casefold(), p[1]))
    loose = sorted(set(loose), key=lambda s: (s.casefold(), s))
    attachments = sorted({h for h in links if ATTACHMENT_RE.search(h)
                          and not h.lower().startswith(("javascript:", "#", "mailto:"))})
    mapped, keys = _map_pairs(pairs, config.field_key)
    return Extraction(source="dom", pairs=pairs, mapped=mapped, pair_keys=keys, loose=loose,
                      attachments=attachments)


def row_items(row: Mapping[str, Any], field_order: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """(LABEL, value) lines for a SODA row: field_order first, then the rest A-Z.

    Socrata ':'-prefixed metadata is dropped; nested objects become COLUMN.KEY.
    """
    clean = {k: v for k, v in row.items() if not str(k).startswith(":")}
    items: List[Tuple[str, str]] = []
    used = set()
    for label, column in field_order:
        used.add(column)
        if column in clean:
            for name, value in flatten({column: clean[column]}):
                items.append((label + name[len(column):].upper(), value))
    rest = {k: v for k, v in clean.items() if k not in used}
    items.extend((name.upper(), value) for name, value in flatten(rest))
    return [(k, v) for k, v in items if v != ""]


def extract_row(row: Mapping[str, Any], config: DetailConfig) -> Extraction:
    columns = config.catalog_columns()
    mapped = {}
    for column, key in columns.items():
        s = _scalar(row.get(column))
        if s:
            mapped[key] = s
    return Extraction(source="row", pairs=row_items(row, config.field_order), mapped=mapped)


# ------------------------------------------------------------------- rendering


def _duplicates_catalog(key: Optional[str], value: str, catalog: Extraction) -> bool:
    """A portal pair repeating the same catalog field with the same value (e.g. Record Status: Issued)."""
    return bool(key) and key in catalog.mapped and fold(catalog.mapped[key]) == fold(value)


def render_content(catalog: Extraction, portal: Optional[Extraction], final_url: str) -> str:
    lines = [f"{label}: {value}" for label, value in catalog.pairs]
    if portal is not None:
        keys = portal.pair_keys or [None] * len(portal.pairs)
        extra = [f"{label}: {value}" for (label, value), key in zip(portal.pairs, keys)
                 if not _duplicates_catalog(key, value, catalog)]
        extra += list(portal.loose)
        if extra:
            lines.append(PORTAL_MARKER)
            lines.extend(extra)
    lines.append(f"SOURCE: {final_url}")
    return "\n".join(lines) + "\n"


def build_fields(catalog: Extraction, portal: Optional[Extraction]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"catalog": dict(sorted(catalog.mapped.items()))}
    if portal is not None:
        fields["portal"] = dict(sorted(portal.mapped.items()))
        fields["portal_pairs"] = [[k, v] for k, v in portal.pairs]
        fields["attachments"] = list(portal.attachments)
        fields["portal_source"] = portal.source
        if portal.record_url:
            fields["portal_record_url"] = portal.record_url
    return fields


def normalize(*, permit: PermitRef, config: DetailConfig, strategy: str, final_url: str,
              html: Optional[str] = None, responses: Sequence[CapturedResponse] = (),
              row: Optional[Mapping[str, Any]] = None,
              portal: Optional[Extraction] = None) -> DetailDocument:
    """Build the DetailDocument. `row` defaults to the permit's stored raw_row.

    soda_row: content = the row. Portal strategies: the stored catalog row plus a
    PORTAL ONLY section from `html`/`responses` (or a precomputed `portal`).
    """
    catalog = extract_row(row if row is not None else permit.raw_row, config)
    if strategy != SODA_ROW and portal is None:
        portal = extract_portal(html, responses, config)
    if strategy == SODA_ROW:
        portal = None
    text = render_content(catalog, portal, final_url)
    return DetailDocument(
        permit_number=permit.permit_number, jurisdiction=permit.jurisdiction, final_url=final_url,
        content_text=text, fields=build_fields(catalog, portal), fetch_strategy=strategy,
        content_hash=content_hash(text),
    )

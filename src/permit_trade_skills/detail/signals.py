"""F2: deterministic relevance & completeness validation of candidate content.

Five signals, scored against the catalog permit:

1. anchor       - the permit number: a mapped record-number field must equal it;
                  without one, the number must appear in the text. Required:
                  a page for a different permit fails regardless of the count.
2. address      - >= 80% of the catalog address tokens appear in the candidate
                  address field (or, without one, the text).
3. date         - some date in the text is within +/- 1 day of a catalog date
                  (issued / applied / completed / expires).
4. description  - the candidate description/scope field shares >= 4 content
                  words (length > 3, not a stopword) with the catalog description,
                  with Jaccard >= 0.3.
5. other        - a status, valuation, trade, classification, scope or
                  inspection field (or token) is present.

Passes when the anchor holds and count >= min_signals (default 4).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Set

from permit_trade_skills.detail.models import PermitRef, SignalReport
from permit_trade_skills.detail.normalize import Extraction, fold

STOPWORDS = {
    "about", "above", "after", "again", "against", "also", "been", "before", "being", "below",
    "between", "both", "does", "doing", "down", "during", "each", "from", "further", "have",
    "having", "here", "into", "itself", "just", "more", "most", "once", "only", "other", "over",
    "same", "should", "some", "such", "than", "that", "their", "them", "then", "there", "these",
    "they", "this", "those", "through", "under", "until", "very", "were", "what", "when", "where",
    "which", "while", "will", "with", "within", "would", "your",
}
OTHER_KEYS = ("status", "valuation", "trades", "classification", "scope", "inspections", "permit_type")
OTHER_TOKENS = re.compile(r"\b(status|valuation|cost|trades?|classification|scope of work|inspections?)\b",
                          re.I)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

ADDRESS_OVERLAP = 0.8
DESC_MIN_SHARED = 4
DESC_MIN_JACCARD = 0.3


def _alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def address_tokens(value: Optional[str]) -> List[str]:
    return re.findall(r"[A-Z0-9]+", (value or "").upper())


def content_words(value: Optional[str]) -> Set[str]:
    words = re.findall(r"[a-z]+", (value or "").lower())
    return {w for w in words if len(w) > 3 and w not in STOPWORDS}


def dates_in(text: str) -> List[date]:
    out = []
    for y, m, d in ISO_DATE.findall(text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for m, d, y in US_DATE.findall(text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def score(candidate: Extraction, permit: PermitRef, min_signals: int = 4) -> SignalReport:
    text = candidate.text
    mapped = candidate.mapped
    details = {}

    # 1. anchor
    number = _alnum(permit.permit_number)
    if mapped.get("permit_number"):
        anchor = _alnum(mapped["permit_number"]) == number
        details["anchor"] = f"record number field {mapped['permit_number']!r}"
    else:
        anchor = bool(number) and number in _alnum(text)
        details["anchor"] = "permit number in text" if anchor else "permit number not found"

    # 2. address
    tokens = address_tokens(permit.address)
    target = set(address_tokens(mapped.get("address") or text))
    overlap = (sum(1 for t in tokens if t in target) / len(tokens)) if tokens else 0.0
    address_ok = overlap >= ADDRESS_OVERLAP
    details["address_overlap"] = round(overlap, 3)

    # 3. date within +/- 1 day
    catalog_dates = [_as_date(d) for d in permit.dates()]
    found = dates_in(text)
    date_ok = any(abs(f - c) <= timedelta(days=1) for f in found for c in catalog_dates)

    # 4. description overlap
    desc = " ".join(v for k, v in mapped.items() if k in ("description", "scope"))
    a, b = content_words(desc), content_words(permit.description)
    shared = len(a & b)
    jaccard = shared / len(a | b) if a | b else 0.0
    desc_ok = shared >= DESC_MIN_SHARED and jaccard >= DESC_MIN_JACCARD
    details["description"] = {"shared": shared, "jaccard": round(jaccard, 3)}

    # 5. any of status / valuation / trade / classification / scope / inspection
    other_ok = any(mapped.get(k) for k in OTHER_KEYS) or bool(OTHER_TOKENS.search(text))

    matched = {"anchor": anchor, "address": address_ok, "date": date_ok,
               "description": desc_ok, "other": other_ok}
    return SignalReport(count=sum(matched.values()), min_signals=min_signals, anchor=anchor,
                        matched=matched, details=details)


def citation_values(permit: PermitRef) -> Iterable[str]:
    """The catalog values Stage 05 AC1 requires verbatim in content_text."""
    yield permit.permit_number
    if permit.address:
        yield permit.address
    if permit.description:
        yield permit.description
    if permit.issued_at:
        yield _as_date(permit.issued_at).isoformat()


def missing_citations(content_text: str, permit: PermitRef) -> List[str]:
    """Catalog values NOT found verbatim (case-insensitive, whitespace-normalized)."""
    haystack = fold(content_text)
    return [v for v in citation_values(permit) if fold(v) not in haystack]

"""K2: deterministic pre-LLM context assembly (no I/O beyond reading the prompt file).

Input: the catalog permit (`PermitInput`) and its stored `DetailDocument`
(Stage 05), or no document in degraded mode.

- Citation substrate: the snapshot's `content_text` (the whole text; quotes are
  checked against all of it), source URL = the snapshot's `final_url`.
  Degraded mode (no snapshot, `allow_degraded`): the catalog fields rendered
  by `catalog_text`, source URL = the permit's `detail_url`.
- Excerpt sent to the model: the whole substrate when it fits in
  `max_context_chars` (default 8,000; Seattle records are 3-5k). Over the
  budget, lines are kept by priority (scope and key catalog lines, then
  trade-signal lines, then other lines, then "No"-answers) and emitted in their
  original order, so quotes that span adjacent lines stay valid.
- Raw HTML is never sent; the detail text is untrusted data, placed inside the
  delimited PERMIT DETAIL block.
- Hashes: `context_hash` = sha256 of the canonical JSON of everything the
  prompt is built from; `substrate_hash` = sha256 of the substrate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from permit_trade_skills.detail.models import DetailDocument
from permit_trade_skills.skills import signals as S
from permit_trade_skills.skills.schemas import PROMPT_VERSION, NotEligible

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEGRADED_ASSUMPTION = "Detail page unavailable; inferred from catalog description only."
KEY_LABELS = {"PERMIT", "STATUS", "ADDRESS", "DESCRIPTION", "CLASSIFICATION", "PERMIT_TYPE", "PERMIT_TYPE_DESC",
              "EST_PROJECT_COST", "ISSUED", "PORTAL ONLY", "SOURCE"}

PERMIT_FIELDS = ("id", "jurisdiction_id", "permit_number", "status", "processing_state", "permit_class",
                 "permit_class_mapped", "permit_type_mapped", "permit_type_desc", "valuation", "issued_at",
                 "address", "city", "state", "zip_code", "description", "detail_url")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PermitInput:
    """The catalog view of one permit, as skill inference needs it."""

    permit_id: int
    jurisdiction: str
    permit_number: str
    status: Optional[str] = None
    processing_state: Optional[str] = None
    permit_class: Optional[str] = None
    permit_class_mapped: Optional[str] = None
    permit_type_mapped: Optional[str] = None
    permit_type_desc: Optional[str] = None
    valuation: Optional[float] = None
    issued_at: Optional[datetime] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    description: Optional[str] = None
    detail_url: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "PermitInput":
        kw = {k: row.get(k) for k in PERMIT_FIELDS if k not in ("id", "jurisdiction_id")}
        return cls(permit_id=row["id"], jurisdiction=row["jurisdiction_id"], **kw)


def _fmt_date(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value) if value else "unknown"


def _fmt_cost(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def catalog_text(permit: PermitInput) -> str:
    """Degraded-mode substrate: the permit's catalog fields as `LABEL: value` lines."""
    rows = [("PERMIT", permit.permit_number), ("STATUS", permit.status), ("ADDRESS", permit.address),
            ("DESCRIPTION", permit.description), ("CLASSIFICATION", permit.permit_class),
            ("CLASSIFICATION_MAPPED", permit.permit_class_mapped), ("PERMIT_TYPE", permit.permit_type_mapped),
            ("PERMIT_TYPE_DESC", permit.permit_type_desc),
            ("ISSUED", _fmt_date(permit.issued_at) if permit.issued_at else None),
            ("EST_PROJECT_COST", permit.valuation)]
    return "\n".join(f"{label}: {' '.join(str(value).split())}" for label, value in rows if value not in (None, ""))


def _label(line: str) -> str:
    return line.split(":", 1)[0].strip().upper() if ":" in line else ""


def _priority(line: str) -> int:
    if S.is_scope_line(line) or _label(line) in KEY_LABELS:
        return 0
    if S.line_trades(line):
        return 1
    if S.is_negative_answer(line):
        return 3
    return 2


def select_excerpt(text: str, max_chars: int) -> Tuple[str, str]:
    """(excerpt, note): the whole text if it fits, else lines by priority in original order."""
    if len(text) <= max_chars:
        return text, ""
    lines = text.splitlines()
    order = sorted(range(len(lines)), key=lambda i: (_priority(lines[i]), i))
    keep, used = set(), 0
    for i in order:
        cost = len(lines[i]) + 1
        if used + cost > max_chars:
            continue
        keep.add(i)
        used += cost
    excerpt = "\n".join(lines[i] for i in sorted(keep))
    note = (f"(The PERMIT DETAIL below is an excerpt: {len(keep)} of {len(lines)} lines, chosen for scope "
            f"and trade relevance; other lines were omitted for length.)\n")
    return excerpt, note


@dataclass
class SkillContext:
    permit: PermitInput
    substrate: str                    # citation substrate (full text)
    source_url: str                   # the one valid evidence_source
    excerpt: str                      # the text sent to the model (a subset of substrate lines)
    excerpt_note: str
    facts: Dict[str, Any]
    signals: Dict[str, S.TradeSignal]
    degraded: bool                    # no detail snapshot: catalog text only
    detail_degraded: bool = False     # snapshot exists but Stage 05 marked it degraded (stale / judge-rescued)
    detail_notes: List[str] = field(default_factory=list)
    input_snapshot_id: Optional[int] = None
    fetch_strategy: Optional[str] = None
    substrate_hash: str = ""
    context_hash: str = ""

    def to_log(self) -> Dict[str, Any]:
        return {"permit": self.permit.permit_number, "degraded": self.degraded,
                "snapshot_id": self.input_snapshot_id, "context_hash": self.context_hash,
                "excerpt_chars": len(self.excerpt), "substrate_chars": len(self.substrate),
                "signals": {t: s.strength for t, s in sorted(self.signals.items())}}


def build_context(permit: PermitInput, detail: Optional[DetailDocument], *, max_chars: int = 8000,
                  allow_degraded: bool = False) -> SkillContext:
    """Assemble the reasoning context. Raises NotEligible when there is nothing citable."""
    if detail is not None:
        substrate, source_url, degraded = detail.content_text, detail.final_url, False
    elif not allow_degraded:
        raise NotEligible(permit.permit_number, "no stored detail snapshot (run `pts detail fetch`, "
                                                "or allow degraded mode)")
    elif not permit.detail_url:
        raise NotEligible(permit.permit_number, "no detail snapshot and no detail_url to cite")
    else:
        substrate, source_url, degraded = catalog_text(permit), permit.detail_url, True
    if not substrate.strip():
        raise NotEligible(permit.permit_number, "empty detail text")

    excerpt, note = select_excerpt(substrate, max_chars)
    facts = {
        "slug": permit.jurisdiction, "permit_number": permit.permit_number, "status": permit.status or "unknown",
        "class": permit.permit_class or permit.permit_class_mapped or "unknown",
        "type_desc": permit.permit_type_desc or permit.permit_type_mapped or "unknown",
        "cost": _fmt_cost(permit.valuation), "issued": _fmt_date(permit.issued_at),
        "address": permit.address or "unknown",
        "scope_lines": [ln.strip() for ln in substrate.splitlines() if S.is_scope_line(ln)],
    }
    ctx = SkillContext(
        permit=permit, substrate=substrate, source_url=source_url, excerpt=excerpt, excerpt_note=note,
        facts=facts, signals=S.detect(substrate), degraded=degraded,
        detail_degraded=bool(detail.degraded) if detail else False,
        detail_notes=list(detail.notes) if detail else [],
        input_snapshot_id=detail.snapshot_id if detail else None,
        fetch_strategy=detail.fetch_strategy if detail else None,
        substrate_hash=sha256(substrate),
    )
    ctx.context_hash = sha256(json.dumps({
        "facts": facts, "excerpt": excerpt, "source_url": source_url, "degraded": degraded,
        "detail_degraded": ctx.detail_degraded, "substrate_hash": ctx.substrate_hash,
    }, sort_keys=True, default=str))
    return ctx


# ---- prompt ---------------------------------------------------------------

_BLOCK = re.compile(r"^## (System|User)\s*\n+```text\n(.*?)\n```", re.S | re.M)


@lru_cache(maxsize=4)
def load_prompt(version: str = PROMPT_VERSION) -> Tuple[str, str]:
    """(system, user_template) from prompts/<version>.md (dashes -> underscores)."""
    path = PROMPTS_DIR / f"{version.replace('-', '_')}.md"
    blocks = dict(_BLOCK.findall(path.read_text(encoding="utf-8")))
    return blocks["System"], blocks["User"]


def render(ctx: SkillContext, version: str = PROMPT_VERSION) -> Tuple[str, str]:
    system, template = load_prompt(version)
    values = {**{k: str(v) for k, v in ctx.facts.items() if k != "scope_lines"},
              "source_url": ctx.source_url, "excerpt_note": ctx.excerpt_note, "content_text": ctx.excerpt}
    user = re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)
    return system, user


def cache_key(snapshot_id: Optional[int], prompt_version: str, model: str, context_hash: str) -> str:
    """K3 step 2: the idempotency key (input_cache_key)."""
    return sha256(f"{snapshot_id if snapshot_id is not None else 'none'}|{prompt_version}|{model}|{context_hash}")

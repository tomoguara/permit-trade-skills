"""K2 trade signals: a curated, transparent keyword -> trade map over the detail text.

Signals are advisory, never the answer. They are used to:

1. pick which lines go into the prompt when the detail text is over the
   context budget (context.py),
2. cross-check the model's output after the call (infer.py): a *high* signal
   with no matching skill adds an assumption and lowers confidence; skills are
   never added from signals,
3. be stored (skill_inferences.signal_check) and logged.

Strength: a trade is a **high** signal when one of its keywords appears on a
scope line (the description / project description / scope of work); a hit
anywhere else is **low**. A form answer that says no ("Fire Alarm: No",
"Is there a spray paint booth?: No") is not a hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

# trade -> keywords (regex alternatives, matched case-insensitively on word boundaries)
TRADE_SIGNALS: Dict[str, Sequence[str]] = {
    "Concrete/Foundation": ("concrete", "foundations?", "slabs?", "footings?"),
    "Framing/Carpentry": ("framing", "studs?", "joists?", "trusses", "truss"),
    "Drywall": ("drywall", "plaster"),
    "Electrical": ("electrical", "panels?", "wiring", "outlets?"),
    "Plumbing": ("plumbing", "supply lines?", "drains?", "drainage", "water heaters?", "sewer"),
    "HVAC": ("hvac", "ducts?", "furnaces?", "mini-split", "refrigerant"),
    "Roofing": ("roof", "roofs", "roofing", "shingles?", "membrane", "flashing"),
    "Siding/Masonry": ("siding", "stucco", "fiber cement", "masonry"),
    "Insulation": ("insulation",),
    "Painting": ("paint", "painting", "stain", "primer"),
    "Glazing/Carpentry": ("windows?", "doors?", "glass", "glazing"),
    "Fireproofing/Spray Foam": ("fire", "sprinklers?", "fireproofing", "fire rating", "fire-rated"),
    "Sheet Metal/HVAC": ("sheet metal", "ductwork", "flues?"),
    "Demolition": ("demolish", "demolition"),
}

# Lines whose label marks them as the permit's scope (the "high" signal source).
SCOPE_LABEL = re.compile(r"^\s*(description|project description|scope( of work)?|work description|"
                         r"type of work|trades?|work)\s*:", re.I)
NEGATIVE_VALUES = {"no", "none", "n/a", "na", "0", "false", "unknown", "-", "--"}

_PATTERNS = {trade: re.compile(r"\b(" + "|".join(words) + r")\b", re.I)
             for trade, words in TRADE_SIGNALS.items()}


def is_scope_line(line: str) -> bool:
    return bool(SCOPE_LABEL.match(line))


def is_negative_answer(line: str) -> bool:
    """A `label: value` line whose value says no / none / zero."""
    if ":" not in line:
        return False
    value = line.rsplit(":", 1)[1].strip().rstrip(".").casefold()
    return value in NEGATIVE_VALUES


@dataclass
class TradeSignal:
    trade: str
    strength: str                       # "high" | "low"
    keywords: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"trade": self.trade, "strength": self.strength, "keywords": self.keywords,
                "lines": self.lines[:3]}


def line_trades(line: str) -> List[str]:
    """Trades with a keyword on this line (negative form answers excluded)."""
    if is_negative_answer(line):
        return []
    return [trade for trade, pat in _PATTERNS.items() if pat.search(line)]


def detect(text: str) -> Dict[str, TradeSignal]:
    """Signals found in `text`, by trade."""
    found: Dict[str, TradeSignal] = {}
    for line in text.splitlines():
        if is_negative_answer(line):
            continue
        scope = is_scope_line(line)
        for trade, pat in _PATTERNS.items():
            hits = sorted({m.group(0).lower() for m in pat.finditer(line)})
            if not hits:
                continue
            sig = found.setdefault(trade, TradeSignal(trade, "low"))
            if scope:
                sig.strength = "high"
            sig.keywords = sorted(set(sig.keywords) | set(hits))
            if line.strip() not in sig.lines:
                sig.lines.append(line.strip())
    return found


def covers(trade: str, signal: TradeSignal, skill_texts: Sequence[str]) -> bool:
    """Does any skill (its skill + trade text) cover this signal's trade?

    Matches the signal trade's name parts (e.g. "Framing", "Carpentry") or the
    keywords that fired, as whole words.
    """
    terms = {p.strip().lower() for p in re.split(r"[/&,]", trade) if p.strip()} | set(signal.keywords)
    terms |= {t.rstrip("s") for t in terms if len(t) > 4}
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms)) + r")", re.I)
    return any(pattern.search(text) for text in skill_texts)


def cross_check(signals: Dict[str, TradeSignal], skills: Sequence[Dict[str, str]]) -> Dict[str, object]:
    """K3 step 6: covered signals and high signals with no skill."""
    texts = [f"{s.get('skill', '')} {s.get('trade', '')}" for s in skills]
    covered = sorted(t for t, sig in signals.items() if covers(t, sig, texts))
    missing_high = sorted(t for t, sig in signals.items() if sig.strength == "high" and t not in covered)
    return {"signals": {t: s.to_dict() for t, s in sorted(signals.items())}, "covered": covered,
            "missing_high_signals": missing_high}

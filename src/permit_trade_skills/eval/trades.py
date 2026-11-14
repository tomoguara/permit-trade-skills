"""The canonical trade vocabulary for golden labels and H2 scoring.

Stage 06 trades are free text by convention ("Carpentry", "Framing/Carpentry",
"Sheet Metal/HVAC", ...). Labels use the canonical names below; the model's
output is mapped onto them with `canonical_trades(trade, skill)`:

1. every canonical trade whose pattern matches the `trade` string (a
   compound such as "Glazing/Carpentry" maps to both trades);
2. if none matches, the patterns are tried on the `skill` phrase;
3. if still none, the trade is kept as `Other: <trade>` (it counts as output,
   and against precision, since no label can contain it).

Order and patterns are data, not logic: extend `TRADES` (and docs/quality.md)
when a new trade shows up in labels or outputs.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Pattern, Set

TRADES: Dict[str, str] = {
    "Carpentry": r"carpent|framing|framer|\bframe\b|cabinet|millwork|\btrim\b|\bdecks?\b|stair|joist|truss|sheathing",
    "Concrete": r"concrete|foundation|footing|\bslab|flatwork|helical|\bpiles?\b|piling|underpinning",
    "Masonry": r"mason|brick|\bblock\b|\bstone\b|chimney",
    "Drywall": r"drywall|gypsum|plaster|wallboard|taping",
    "Electrical": r"electric|wiring|lighting|low[- ]voltage|solar|photovoltaic|\bpv\b",
    "Plumbing": r"plumb|\bdrain|sewer|water heater|\bgas pip",
    "HVAC": r"hvac|mechanical|heating|ventilat|air[- ]condition|\bducts?\b|ductwork|sheet metal|heat pump|furnace",
    "Roofing": r"\b(re-?)?roof|shingle",
    "Siding": r"siding|cladding|stucco|envelope|weather[- ]?(proof|barrier)|waterproof",
    "Insulation": r"insulat",
    "Painting": r"paint|coating|\bstain",
    "Windows/Doors": r"window|\bdoors?\b|glazing|\bglass|skylight|storefront",
    "Fire Protection": r"fire[- ]?(sprinkler|alarm|protection|proof|stop)|sprinkler|fireproof",
    "Demolition": r"demoli|\bdemo\b|deconstruct|tear[- ]?(down|off)|removal of (the )?(existing )?(structure|building|dwelling)",
    "Abatement": r"abatement|asbestos|hazardous material",
    "Excavation": r"excavat|earthwork|grading|site ?work|shoring|trench|backfill",
    "Paving": r"paving|asphalt|striping",
    "Structural Steel": r"steel|weld|ironwork",
    "Flooring": r"flooring|\btile\b|tiling|carpet",
    "Landscaping": r"landscap|planting|irrigation",
    "Elevator": r"elevator|conveying",
    "General Construction": r"general (contract|construction|building)|construction management",
}
_COMPILED: Dict[str, Pattern[str]] = {name: re.compile(p, re.I) for name, p in TRADES.items()}
OTHER_PREFIX = "Other: "
_BY_LOWER = {name.lower(): name for name in TRADES}


def canonical_name(label: str) -> str:
    """A label's trade spelled canonically (case-insensitive exact name, else mapped, else unchanged)."""
    text = (label or "").strip()
    if text.lower() in _BY_LOWER:
        return _BY_LOWER[text.lower()]
    mapped = match(text)
    return mapped[0] if len(mapped) == 1 else text


def match(text: str) -> List[str]:
    return [name for name, pattern in _COMPILED.items() if pattern.search(text or "")]


def canonical_trades(trade: str, skill: str = "") -> Set[str]:
    found = match(trade)
    if not found:
        found = match(skill)
    if not found:
        found = [OTHER_PREFIX + " ".join((trade or skill or "?").split())]
    return set(found)


def output_trades(skills: Iterable[Dict[str, str]]) -> Set[str]:
    """Canonical trades of an inference's (accepted) skills."""
    out: Set[str] = set()
    for s in skills:
        out |= canonical_trades(s.get("trade") or "", s.get("skill") or "")
    return out


def unknown_labels(trades: Iterable[str]) -> List[str]:
    """Label values that are not canonical trade names (a labeling typo)."""
    return [t for t in trades if t not in TRADES]

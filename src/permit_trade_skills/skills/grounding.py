"""K3 step 5: the grounding gate, the only path to an accepted skill_inferences row.

Per skill (hard checks; a failing skill is dropped):

- `evidence`, normalized, is a substring of the citation substrate, normalized.
  Normalization = NFKC, remove soft hyphens, casefold, collapse whitespace.
  Exact substring only, no fuzzy matching. Wrapping quote marks around the
  evidence are ignored.
- `evidence_source` equals the substrate's source URL (the snapshot's
  `final_url`; in degraded mode the permit's `detail_url`).
- `evidence` does not look like an instruction to the model (prompt-injection
  guard: the quote passes the substring check because the injected text *is*
  in the page, but it is never evidence of a trade skill).

Per skill (soft checks; logged in warnings, never drop): `skill` is a
capability phrase: at least 2 words, not equal to `trade`, not ending in
"license"/"certification".

Acceptance: all pass -> accept. Some fail -> accept the survivors, list the
others in `dropped`, cap overall confidence at medium (infer.py). None pass ->
reject the whole inference (GROUNDING_FAILED).

Document check (before the LLM call): the substrate must mention the permit
number, otherwise it is another permit's detail and nothing is inferred.

Stage 08 re-runs `check_skill` on stored rows; docs/trade-skills.md states the same
rule for human reviewers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

_WS = re.compile(r"\s+")
_QUOTES = "\"'“”‘’"
INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|earlier)\b.{0,40}"
    r"\b(instructions?|rules?|prompts?)\b"
    r"|\b(system prompt|you are now|new instructions?|as an ai)\b"
    r"|\b(add|include|output|list)\b.{0,30}\b(skill|trade)s?\b.{0,30}\b(regardless|anyway|even if)\b",
    re.I | re.S)
LICENSE_SUFFIX = re.compile(r"\b(license|licence|licensing|certification|certificate)\s*$", re.I)

# drop reasons
NOT_VERBATIM = "evidence_not_verbatim"
WRONG_SOURCE = "evidence_source_mismatch"
INJECTED = "evidence_is_instruction"


def normalize(text: str) -> str:
    """NFKC, soft hyphens removed, casefolded, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text or "").replace("­", "")
    return _WS.sub(" ", text.casefold()).strip()


def clean_evidence(evidence: str) -> str:
    return (evidence or "").strip().strip(_QUOTES).strip()


def _alnum(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def mentions_permit(substrate: str, permit_number: str) -> bool:
    """Document anchor: the substrate names this permit (punctuation-insensitive)."""
    number = _alnum(permit_number)
    return bool(number) and number in _alnum(substrate)


def soft_warnings(skill: Dict[str, Any]) -> List[str]:
    name = (skill.get("skill") or "").strip()
    out = []
    if len(name.split()) < 2:
        out.append("not a capability phrase (one word)")
    if normalize(name) == normalize(skill.get("trade") or ""):
        out.append("skill equals its trade")
    if LICENSE_SUFFIX.search(name):
        out.append("names a license/certification, not a capability")
    return out


def check_skill(skill: Dict[str, Any], substrate: str, source_url: str, *,
                normalized_substrate: str = "") -> List[str]:
    """Hard-check failures for one skill ([] = grounded)."""
    reasons = []
    evidence = clean_evidence(skill.get("evidence") or "")
    haystack = normalized_substrate or normalize(substrate)
    if not evidence or normalize(evidence) not in haystack:
        reasons.append(NOT_VERBATIM)
    if (skill.get("evidence_source") or "").strip() != source_url:
        reasons.append(WRONG_SOURCE)
    if INJECTION.search(evidence):
        reasons.append(INJECTED)
    return reasons


@dataclass
class GroundingReport:
    accepted: List[Dict[str, Any]] = field(default_factory=list)
    dropped: List[Dict[str, Any]] = field(default_factory=list)     # {skill..., reasons: [...]}
    warnings: List[str] = field(default_factory=list)               # soft-check notes
    checked: int = 0

    @property
    def passed(self) -> bool:
        """At least one skill survived (the inference can be accepted)."""
        return bool(self.accepted)

    @property
    def first_pass_rate(self) -> float:
        return len(self.accepted) / self.checked if self.checked else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"checked": self.checked, "accepted": len(self.accepted), "dropped": self.dropped,
                "warnings": self.warnings}


def gate(skills: Sequence[Dict[str, Any]], substrate: str, source_url: str) -> GroundingReport:
    """Apply the hard checks to every skill; collect soft warnings for the survivors."""
    report = GroundingReport(checked=len(skills))
    haystack = normalize(substrate)
    for skill in skills:
        reasons = check_skill(skill, substrate, source_url, normalized_substrate=haystack)
        if reasons:
            report.dropped.append({**skill, "reasons": reasons})
            continue
        report.accepted.append({**skill, "evidence": clean_evidence(skill["evidence"])})
        report.warnings += [f"{skill.get('skill')!r}: {w}" for w in soft_warnings(skill)]
    return report


_QUOTED = re.compile(r"[\"“'‘]([^\"“”'‘’]{8,})[\"”'’]")


def licensing_note_grounded(note: str, substrate: str) -> bool:
    """Kept when it starts with "assumed", is itself verbatim detail text, or quotes the detail verbatim."""
    if note.strip().lower().startswith("assumed"):
        return True
    haystack = normalize(substrate)
    return normalize(note) in haystack or any(normalize(q) in haystack for q in _QUOTED.findall(note))


def verify_row(skills: Sequence[Dict[str, Any]], substrate: str, source_url: str) -> List[Dict[str, Any]]:
    """Per-skill ✓/✗ for a stored inference (pts skills show, Stage 08 H1)."""
    haystack = normalize(substrate)
    return [{"skill": s.get("skill"), "trade": s.get("trade"), "evidence": s.get("evidence"),
             "confidence": s.get("confidence"),
             "reasons": check_skill(s, substrate, source_url, normalized_substrate=haystack)}
            for s in skills]

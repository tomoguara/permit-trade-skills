"""Stage 06: trade-skill inference (the reasoning core).

Given an issued permit and its stored detail document (Stage 05), infer the
trade skills needed to carry out the permitted work. Every skill quotes the
detail text verbatim (`evidence`) and names its source URL
(`evidence_source`); the grounding gate (grounding.py) drops any skill whose
quote is not in the stored text, and is the only path to an accepted
skill_inferences row.

Modules: schemas (output contract, errors, result), signals (keyword -> trade
map), context (deterministic prompt context), grounding (the gate), infer
(`infer_skills`, the pipeline), persist (port + audit row), service (CLI /
Stage 07 facade). Prompt: prompts/skill_infer_v1.md.

This package never imports `permit_trade_skills.store`; build a wired service
with `permit_trade_skills.wiring.skill_service()`. See docs/trade-skills.md.
"""

from permit_trade_skills.skills.infer import infer_skills
from permit_trade_skills.skills.schemas import (
    PROMPT_VERSION,
    InferenceFailed,
    NotEligible,
    SkillInference,
)

__all__ = ["PROMPT_VERSION", "InferenceFailed", "NotEligible", "SkillInference", "infer_skills"]

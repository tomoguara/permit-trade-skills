"""K1 output contract: JSON schemas, Pydantic models, the SkillInference result and typed errors.

Two schemas describe the same output:

- `OUTPUT_SCHEMA`: the full contract (lengths, item counts). Pydantic
  (`SkillOutput`) enforces it after every call; docs/trade-skills.md documents it.
- `provider_schema(source_url)`: the form sent to the provider as
  `response_schema`. It is OpenAI strict-mode compatible (every property
  required, no string-length or item-count keywords) and pins
  `evidence_source` to the one allowed URL with an `enum`, so the model cannot
  garble a long portal URL. `skills` may be empty here: an empty list means
  "nothing in the detail supports a skill" and fails the inference as
  NO_SKILLS instead of being retried as a schema error.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "skill-infer-v1"
CONFIDENCE_LEVELS = ("low", "medium", "high")   # ascending
Confidence = Literal["high", "medium", "low"]
# The trade names prompts/skill_infer_v1.md asks the model to use ("trade" is free
# text, so stored values may include others). The query trade filter offers these.
TRADES = ("Concrete", "Carpentry", "Electrical", "Plumbing", "HVAC", "Drywall", "Insulation",
          "Painting", "Foundation", "Framing", "Roofing", "Siding", "Glazing", "Fireproofing",
          "Sheet Metal", "Demolition")

OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["skills", "assumptions", "overall_confidence"],
    "properties": {
        "skills": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["skill", "trade", "evidence", "evidence_source", "confidence"],
            "properties": {
                "skill": {"type": "string", "minLength": 8, "maxLength": 240},
                "trade": {"type": "string", "maxLength": 80},
                "evidence": {"type": "string", "minLength": 3, "maxLength": 400},
                "evidence_source": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            }}},
        "assumptions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "overall_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "licensing_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
}

_STRICT_DROP = ("minLength", "maxLength", "minItems", "maxItems")


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in _STRICT_DROP}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def provider_schema(source_url: str) -> Dict[str, Any]:
    """The response_schema for one call: strict-mode compatible, evidence_source pinned."""
    schema = _strip(copy.deepcopy(OUTPUT_SCHEMA))
    schema["required"] = sorted(schema["properties"])          # strict mode: all properties required
    schema["properties"]["skills"]["items"]["properties"]["evidence_source"] = {
        "type": "string", "enum": [source_url]}
    return schema


class SkillItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=8, max_length=240)
    trade: str = Field(max_length=80)
    evidence: str = Field(min_length=3, max_length=400)
    evidence_source: str
    confidence: Confidence


class SkillOutput(BaseModel):
    """The model's answer, validated against OUTPUT_SCHEMA's constraints."""

    model_config = ConfigDict(extra="forbid")

    skills: List[SkillItem] = Field(min_length=1, max_length=20)
    assumptions: List[str] = Field(default_factory=list, max_length=10)
    overall_confidence: Confidence
    licensing_notes: List[str] = Field(default_factory=list, max_length=6)


def lower(a: str, b: str) -> str:
    """The lower of two confidence levels."""
    return min(a, b, key=CONFIDENCE_LEVELS.index)


def step_down(level: str) -> str:
    return CONFIDENCE_LEVELS[max(0, CONFIDENCE_LEVELS.index(level) - 1)]


# ---- errors ---------------------------------------------------------------

# InferenceFailed.code values (also stored in permits.state_error.error)
GROUNDING_FAILED = "GROUNDING_FAILED"        # no skill passed the gate, or the detail is another permit's
SCHEMA_INVALID = "SCHEMA_INVALID"            # invalid output twice (first try + one corrective retry)
NO_SKILLS = "NO_SKILLS"                      # the model found nothing in the detail to support a skill
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class NotEligible(Exception):
    """The permit may not be inferred (status gate, missing detail). No LLM call, no state change."""

    def __init__(self, permit_number: str, reason: str):
        super().__init__(f"{permit_number}: {reason}")
        self.permit_number, self.reason = permit_number, reason


class InferenceFailed(Exception):
    """Inference ran and failed. The permit is marked SKILLS_FAILED; nothing was accepted."""

    def __init__(self, permit_number: str, code: str, message: str, *, retryable: bool,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(f"{permit_number}: {code}: {message} (retryable={retryable})")
        self.permit_number, self.code, self.message = permit_number, code, message
        self.retryable = retryable
        self.details = details or {}

    def state_error(self) -> Dict[str, Any]:
        return {"stage": "skill_infer", "error": self.code, "message": self.message,
                "retryable": self.retryable, **self.details}


# ---- result ---------------------------------------------------------------

@dataclass
class SkillInference:
    """One accepted inference (a skill_inferences row). `to_contract()` is overview §5's shape."""

    permit_number: str
    jurisdiction: str
    model: str
    prompt_version: str
    skills: List[Dict[str, Any]]
    assumptions: List[str]
    overall_confidence: str
    evidence_source: str
    degraded: bool = False
    licensing_notes: List[str] = field(default_factory=list)
    dropped_skills: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    signal_check: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None
    prompt_hash: Optional[str] = None
    completion_hash: Optional[str] = None
    context_hash: Optional[str] = None
    input_cache_key: Optional[str] = None
    input_snapshot_id: Optional[int] = None
    substrate_hash: Optional[str] = None
    review_status: str = "PENDING"
    generated_at: Optional[str] = None
    inference_id: Optional[int] = None
    cached: bool = False                    # True: returned from the store, no LLM call
    tokens: Dict[str, int] = field(default_factory=dict)
    cost: Optional[float] = None

    @property
    def permit_key(self) -> str:
        return f"{self.jurisdiction}:{self.permit_number}"

    def to_contract(self) -> Dict[str, Any]:
        out = {"permit_key": self.permit_key, "model": self.model, "prompt_version": self.prompt_version,
               "generated_at": self.generated_at, "skills": self.skills, "assumptions": self.assumptions,
               "overall_confidence": self.overall_confidence, "review_status": self.review_status}
        if self.licensing_notes:
            out["licensing_notes"] = self.licensing_notes
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["permit_key"] = self.permit_key
        return d

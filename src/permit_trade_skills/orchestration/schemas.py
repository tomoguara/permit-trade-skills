"""JSON schemas of the planner: the per-iteration action and the AgentReport.

The provider is single-turn (`complete(system, user)`), so the loop is a
JSON next-action loop: every iteration the model returns one `ACTION`, either
a tool call (`tool` + `arguments_json`, a JSON object as a string) or the
final answer. The schema is OpenAI strict-mode compatible (every property
required, nullable via anyOf, no free-form objects).

`AGENT_REPORT_SCHEMA` is what `Planner.run` returns; the run's `steps`,
`integrity` and budget fields are filled by code, never by the model.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

CONFIDENCE = ["high", "medium", "low"]

_FINDING_SKILL = {
    "type": "object", "additionalProperties": False,
    "required": ["skill", "trade", "evidence", "confidence"],
    "properties": {"skill": {"type": "string"}, "trade": {"type": "string"}, "evidence": {"type": "string"},
                   "confidence": {"type": "string", "enum": CONFIDENCE}},
}
_FINDING = {
    "type": "object", "additionalProperties": False,
    "required": ["permit_number", "status", "detail_present", "skills"],
    "properties": {"permit_number": {"type": "string"}, "status": {"type": "string"},
                   "detail_present": {"type": "boolean"},
                   "skills": {"type": "array", "items": _FINDING_SKILL}},
}
_UNFULFILLED = {
    "type": "object", "additionalProperties": False,
    "required": ["requested", "reason", "suggested_action"],
    "properties": {"requested": {"type": "string"}, "reason": {"type": "string"},
                   "suggested_action": {"type": "string"}},
}
_CITATION = {
    "type": "object", "additionalProperties": False,
    "required": ["claim_index", "source"],
    "properties": {"claim_index": {"type": "integer"},
                   "source": {"type": "string", "description": "tool:<tool name>:<permit number or 'query'>"}},
}
FINAL_ANSWER = {
    "type": "object", "additionalProperties": False,
    "required": ["findings", "unfulfilled", "confidence", "citations"],
    "properties": {
        "findings": {"anyOf": [{"type": "array", "items": _FINDING}, {"type": "null"}]},
        "unfulfilled": {"type": "array", "items": _UNFULFILLED},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "citations": {"type": "array", "items": _CITATION},
    },
}


def action_schema(tool_names: Sequence[str]) -> Dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["thought", "action", "tool", "arguments_json", "final_answer"],
        "properties": {
            "thought": {"type": "string", "description": "One or two sentences: why this step"},
            "action": {"type": "string", "enum": ["tool_call", "final_answer"]},
            "tool": {"anyOf": [{"type": "string", "enum": list(tool_names)}, {"type": "null"}]},
            "arguments_json": {"anyOf": [{"type": "string"}, {"type": "null"}],
                               "description": "The tool arguments as a JSON object string"},
            "final_answer": {"anyOf": [FINAL_ANSWER, {"type": "null"}]},
        },
    }


AGENT_REPORT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["run_id", "goal", "jurisdiction", "toolset_version", "steps", "findings", "unfulfilled",
                 "confidence", "citations", "integrity", "violations", "budget", "stopped"],
    "properties": {
        "run_id": {"type": "string"},
        "goal": {"type": "string"},
        "jurisdiction": {"type": ["string", "null"]},
        "toolset_version": {"type": "string"},
        "prompt_version": {"type": "string"},
        "model": {"type": ["string", "null"]},
        "steps": {"type": "array", "items": {
            "type": "object", "required": ["tool", "params", "ok", "latency_ms"],
            "properties": {"tool": {"type": "string"}, "params": {"type": "object"}, "ok": {"type": "boolean"},
                           "latency_ms": {"type": "integer"}, "error_code": {"type": ["string", "null"]}}}},
        "findings": FINAL_ANSWER["properties"]["findings"],
        "unfulfilled": FINAL_ANSWER["properties"]["unfulfilled"],
        "confidence": FINAL_ANSWER["properties"]["confidence"],
        "citations": FINAL_ANSWER["properties"]["citations"],
        "integrity": {"type": "string", "enum": ["PASSED", "FAILED"]},
        "violations": {"type": "array"},
        "warnings": {"type": "array"},
        "budget": {"type": "object"},
        "llm": {"type": "object"},
        "stopped": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["reason"]}]},
    },
}

"""Provider-agnostic LLM abstraction (overview §6).

Every LLM call in the system goes through `LLMProvider.complete`. Concrete
providers:

- `OpenAICompatibleProvider` (llm/openai_compat.py): any OpenAI-compatible
  chat-completions endpoint. Two profiles are configured in
  config/settings.yaml: `openai` (e.g. gpt-4.1-mini) and `ollama` (local, e.g.
  qwen3.8:latest). `llm.use_local_ollama_model` picks the profile.
- `FakeProvider` (llm/fake.py): canned responses for tests and load tests.

`get_provider(role)` returns the configured provider for a role
(`nlu.parser`, later `skills.infer`, `skills.judge`, `agent.planner`).

Failures: transport errors, 429 and 5xx are retried (`llm.max_retries`, default
2, exponential backoff); then `LLMUnavailable` (retryable) is raised. A
response that is not valid JSON for `response_schema` raises
`LLMResponseInvalid`. Callers must surface these as explicit errors and never
substitute a guessed answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """A function-calling tool: name, LLM-facing description, JSON-schema parameters."""

    name: str
    description: str
    parameters: Dict[str, Any]


@dataclass
class Completion:
    text: str                       # raw assistant text
    parsed: Optional[Any]           # JSON validated against response_schema (None without one)
    raw: Dict[str, Any]             # provider response body
    model: str                      # "provider/model"
    tokens: Dict[str, int] = field(default_factory=dict)  # {"input": n, "output": n}
    cost: Optional[float] = None    # USD, when the profile has a price for the model
    latency_ms: int = 0
    request_id: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


class LLMError(Exception):
    """Base class for provider failures. `retryable` tells callers whether to retry later."""

    retryable = True

    def __init__(self, message: str, *, request_id: str = ""):
        super().__init__(message)
        self.request_id = request_id


class LLMUnavailable(LLMError):
    """Provider unreachable, timed out, rate-limited or 5xx after all retries."""


class LLMResponseInvalid(LLMError):
    """Provider answered, but not with JSON matching the requested schema."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, *, system: str, user: str,
                 tools: Optional[List[ToolSpec]] = None,
                 response_schema: Optional[Dict[str, Any]] = None,
                 model: Optional[str] = None,
                 temperature: float = 0.0,
                 max_tokens: Optional[int] = None,
                 prompt_version: Optional[str] = None) -> Completion: ...


def get_provider(role: str, settings: Optional[Dict[str, Any]] = None) -> LLMProvider:
    """The provider configured for `role`, bound to that role's model.

    `llm.use_local_ollama_model` selects the `ollama` profile (true) or the
    `openai` profile (false).
    """
    from permit_trade_skills.llm.openai_compat import OpenAICompatibleProvider
    from permit_trade_skills.settings import load_settings

    settings = settings or load_settings()
    llm = settings["llm"]
    profile_name = "ollama" if llm["use_local_ollama_model"] else "openai"
    return OpenAICompatibleProvider.from_profile(profile_name, llm["providers"][profile_name], role,
                                                 timeout_s=llm["timeout_s"],
                                                 max_retries=llm["max_retries"])


__all__ = ["Completion", "LLMError", "LLMProvider", "LLMResponseInvalid", "LLMUnavailable",
           "ToolSpec", "get_provider"]

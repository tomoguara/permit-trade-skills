"""Deterministic fake provider for tests, evals with fixtures, and load tests."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Union

import jsonschema

from permit_trade_skills.llm import Completion, LLMResponseInvalid, ToolSpec

Response = Union[Dict[str, Any], Exception]
Responder = Callable[[str, str], Response]


def by_substring(mapping: Dict[str, Response]) -> Responder:
    """Responder that answers with the first value whose key occurs in the user prompt."""

    def respond(system: str, user: str) -> Response:
        for key, value in mapping.items():
            if key in user:
                return value
        raise KeyError(f"FakeProvider: no canned response for {user[:80]!r}")

    return respond


class FakeProvider:
    """Returns canned JSON (or raises a canned exception) for each call.

    `responses` is a callable (system, user) -> dict | Exception (see
    `by_substring`), or a single dict / exception used for every call. Each call
    is recorded in `calls`.
    """

    name = "fake"

    def __init__(self, responses: Union[Responder, Response],
                 *, latency_s: float = 0.0, model: str = "fake-nlu"):
        self.responses = responses
        self.latency_s = latency_s
        self.default_model = model
        self.calls: List[Dict[str, Any]] = []

    @property
    def model_id(self) -> str:
        return f"fake/{self.default_model}"

    def _pick(self, system: str, user: str) -> Response:
        return self.responses(system, user) if callable(self.responses) else self.responses

    def complete(self, *, system: str, user: str,
                 tools: Optional[List[ToolSpec]] = None,
                 response_schema: Optional[Dict[str, Any]] = None,
                 model: Optional[str] = None,
                 temperature: float = 0.0,
                 max_tokens: Optional[int] = None,
                 prompt_version: Optional[str] = None) -> Completion:
        self.calls.append({"system": system, "user": user, "prompt_version": prompt_version})
        if self.latency_s:
            time.sleep(self.latency_s)
        result = self._pick(system, user)
        if isinstance(result, Exception):
            raise result
        if response_schema is not None:
            try:
                jsonschema.validate(result, response_schema)
            except jsonschema.ValidationError as e:
                raise LLMResponseInvalid(f"fake: response does not match the schema: {e.message}") from e
        return Completion(text=json.dumps(result), parsed=result, raw={"fake": True},
                          model=self.model_id, tokens={"input": 0, "output": 0}, cost=0.0,
                          latency_ms=int(self.latency_s * 1000), request_id=uuid.uuid4().hex)

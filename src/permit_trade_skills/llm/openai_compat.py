"""OpenAI-compatible chat-completions provider (OpenAI, Ollama, vLLM, ...)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
import jsonschema

from permit_trade_skills.config import load_env
from permit_trade_skills.llm import (
    Completion,
    LLMResponseInvalid,
    LLMUnavailable,
    ToolSpec,
)

log = logging.getLogger("pts.llm")

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.S)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def extract_json(text: str) -> Any:
    """Parse JSON from assistant text, tolerating ```json fences and <think> blocks."""
    cleaned = _FENCE.sub("", _THINK.sub("", text).strip()).strip()
    return json.loads(cleaned)


class OpenAICompatibleProvider:
    def __init__(self, name: str, base_url: str, default_model: str, *,
                 api_key: Optional[str] = None, timeout_s: float = 120, max_retries: int = 2,
                 prices: Optional[Dict[str, Dict[str, float]]] = None,
                 reasoning_effort: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None,
                 backoff_s: float = 0.5):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.prices = prices or {}
        self.reasoning_effort = reasoning_effort
        self.backoff_s = backoff_s
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_profile(cls, name: str, profile: Dict[str, Any], role: str, *,
                     timeout_s: float = 120, max_retries: int = 2) -> "OpenAICompatibleProvider":
        load_env()
        models = profile.get("models") or {}
        if role not in models:
            raise KeyError(f"no model configured for role {role!r} in llm.providers.{name}.models")
        key_env = profile.get("api_key_env")
        return cls(name, profile["base_url"], models[role],
                   api_key=os.environ.get(key_env) if key_env else None,
                   timeout_s=timeout_s, max_retries=max_retries,
                   prices=profile.get("prices"), reasoning_effort=profile.get("reasoning_effort"))

    @property
    def model_id(self) -> str:
        return f"{self.name}/{self.default_model}"

    def _cost(self, model: str, tokens: Dict[str, int]) -> Optional[float]:
        price = self.prices.get(model)
        if not price:
            return 0.0 if self.name == "ollama" else None
        return round((tokens.get("input", 0) * price["input"]
                      + tokens.get("output", 0) * price["output"]) / 1_000_000, 8)

    def _post(self, body: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "X-Request-Id": request_id}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/v1/chat/completions"
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self.backoff_s * (2 ** (attempt - 1)))
            try:
                resp = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_error = f"{e.__class__.__name__}: {e}"
                continue
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            if resp.status_code >= 400:
                # 4xx other than the retryable ones: bad request/model/key. Not retried.
                raise LLMUnavailable(f"{self.name} rejected the request: HTTP {resp.status_code}: "
                                     f"{resp.text[:200]}", request_id=request_id)
            try:
                return resp.json()
            except ValueError:
                last_error = "response body is not JSON"
        raise LLMUnavailable(f"{self.name} unavailable after {self.max_retries + 1} attempt(s): "
                             f"{last_error}", request_id=request_id)

    def complete(self, *, system: str, user: str,
                 tools: Optional[List[ToolSpec]] = None,
                 response_schema: Optional[Dict[str, Any]] = None,
                 model: Optional[str] = None,
                 temperature: float = 0.0,
                 max_tokens: Optional[int] = None,
                 prompt_version: Optional[str] = None) -> Completion:
        model = model or self.default_model
        request_id = uuid.uuid4().hex
        body: Dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if response_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "response", "strict": True, "schema": response_schema}}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools]

        started = time.monotonic()
        raw = self._post(body, request_id)
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMResponseInvalid(f"{self.name}: malformed response ({e})", request_id=request_id) from e
        text = message.get("content") or ""
        usage = raw.get("usage") or {}
        tokens = {"input": int(usage.get("prompt_tokens", 0)), "output": int(usage.get("completion_tokens", 0))}

        parsed = None
        if response_schema is not None:
            try:
                parsed = extract_json(text)
                jsonschema.validate(parsed, response_schema)
            except (ValueError, jsonschema.ValidationError) as e:
                raise LLMResponseInvalid(f"{self.name}: response does not match the schema: "
                                         f"{str(e).splitlines()[0][:200]}", request_id=request_id) from e

        completion = Completion(
            text=text, parsed=parsed, raw=raw, model=f"{self.name}/{raw.get('model', model)}",
            tokens=tokens, cost=self._cost(model, tokens), latency_ms=latency_ms,
            request_id=request_id, tool_calls=message.get("tool_calls") or [],
        )
        log.info(json.dumps({
            "event": "llm.complete", "request_id": request_id, "model": completion.model,
            "prompt_version": prompt_version, "input_hash": _sha(system + "\n" + user),
            "output_hash": _sha(text), "tokens": tokens, "cost": completion.cost,
            "latency_ms": latency_ms,
        }))
        return completion

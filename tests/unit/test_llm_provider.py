"""LLM abstraction (overview §6): OpenAI-compatible provider, retries, schema validation,
and the use_local_ollama_model switch. Uses httpx.MockTransport (no network)."""

import json

import httpx
import pytest

from permit_trade_skills.llm import LLMResponseInvalid, LLMUnavailable, get_provider
from permit_trade_skills.llm.openai_compat import OpenAICompatibleProvider, extract_json
from permit_trade_skills.settings import DEFAULTS, _merge, load_settings

SCHEMA = {"type": "object", "properties": {"status": {"type": ["string", "null"]}},
          "required": ["status"], "additionalProperties": False}


def reply(content, status=200):
    return httpx.Response(status, json={
        "model": "m", "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def provider(handler, **kw):
    return OpenAICompatibleProvider("openai", "https://llm.test", "gpt-4o-mini", api_key="sk-test",
                                    transport=httpx.MockTransport(handler), backoff_s=0,
                                    prices={"gpt-4o-mini": {"input": 0.15, "output": 0.60}}, **kw)


def test_request_shape_and_parsed_completion():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return reply('{"status": "ISSUED"}')

    c = provider(handler).complete(system="sys", user="usr", response_schema=SCHEMA)
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert body["temperature"] == 0.0 and body["model"] == "gpt-4o-mini"
    assert body["response_format"]["json_schema"] == {"name": "response", "strict": True, "schema": SCHEMA}
    assert "reasoning_effort" not in body
    assert c.parsed == {"status": "ISSUED"}
    assert c.tokens == {"input": 100, "output": 20}
    assert c.cost == pytest.approx((100 * 0.15 + 20 * 0.60) / 1e6)
    assert c.model == "openai/m" and c.request_id


def test_retries_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return reply("", 503) if len(calls) < 3 else reply('{"status": null}')

    assert provider(handler).complete(system="s", user="u", response_schema=SCHEMA).parsed == {"status": None}
    assert len(calls) == 3   # 1 attempt + 2 retries


def test_exhausted_retries_raise_unavailable():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMUnavailable) as info:
        provider(handler).complete(system="s", user="u")
    assert info.value.retryable and len(calls) == 3


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMUnavailable):
        provider(handler).complete(system="s", user="u")
    assert len(calls) == 1


@pytest.mark.parametrize("content", ["not json", '{"status": 5}', '{"other": 1}'])
def test_schema_violation_raises_invalid(content):
    with pytest.raises(LLMResponseInvalid):
        provider(lambda r: reply(content)).complete(system="s", user="u", response_schema=SCHEMA)


def test_extract_json_tolerates_fences_and_think_blocks():
    assert extract_json('<think>hmm</think>\n```json\n{"a": 1}\n```') == {"a": 1}


def test_reasoning_effort_is_sent_when_configured():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply("{}")

    provider(handler, reasoning_effort="none").complete(system="s", user="u")
    assert seen["reasoning_effort"] == "none"


@pytest.mark.parametrize("local,name,base,model", [
    (True, "ollama", "http://localhost:11434", "qwen3.8:latest"),
    (False, "openai", "https://api.openai.com", "gpt-4.1-mini"),
])
def test_use_local_ollama_model_selects_profile(local, name, base, model):
    settings = _merge(DEFAULTS, {"llm": {"use_local_ollama_model": local}})
    p = get_provider("nlu.parser", settings)
    assert (p.name, p.base_url, p.default_model) == (name, base, model)


def test_env_override_and_settings_file(tmp_path, monkeypatch):
    f = tmp_path / "settings.yaml"
    f.write_text("llm:\n  use_local_ollama_model: false\napi:\n  rate_limit_per_minute: 5\n")
    monkeypatch.delenv("PTS_USE_LOCAL_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("PTS_RATE_LIMIT_PER_MINUTE", raising=False)
    s = load_settings(f)
    assert s["llm"]["use_local_ollama_model"] is False and s["api"]["rate_limit_per_minute"] == 5
    assert s["llm"]["providers"]["ollama"]["base_url"] == "http://localhost:11434"   # defaults kept
    monkeypatch.setenv("PTS_USE_LOCAL_OLLAMA_MODEL", "true")
    assert load_settings(f)["llm"]["use_local_ollama_model"] is True


def test_unknown_role_is_an_error():
    with pytest.raises(KeyError):
        get_provider("no.such.role", DEFAULTS)


def test_skills_infer_role_is_configured_in_both_profiles():
    for profile, model in (("openai", "gpt-4.1"), ("ollama", "qwen3.8:latest")):
        assert DEFAULTS["llm"]["providers"][profile]["models"]["skills.infer"] == model

"""The API providers (no GPU): Anthropic and OpenAI, with fake SDK clients."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from app.llm.anthropic import REFUSAL_FALLBACK_BETA, AnthropicProvider
from app.llm.base import LLMError, LLMMessage
from app.llm.openai import OpenAIProvider
from app.llm.schema import strict_schema

MESSAGES = [LLMMessage("system", "be careful"), LLMMessage("user", "why did it fail?")]
ANSWER = {"root_cause": "react version conflict"}
SCHEMA = {
    "type": "object",
    "title": "Analysis",
    "properties": {
        "root_cause": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {"quote": {"type": "string"}}}},
    },
    "required": ["root_cause"],
}


class FakeCalls:
    """Records calls and returns queued responses (an exception is raised instead)."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def anthropic_reply(payload=ANSWER, *, stop_reason="end_turn", category=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
    )


def openai_reply(payload=ANSWER, *, finish_reason="stop", refusal=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=text, refusal=refusal))],
        model="gpt-4o",
        usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=300),
    )


def anthropic_provider(beta_outcomes, plain_outcomes=(), **kwargs):
    beta, plain = FakeCalls(list(beta_outcomes)), FakeCalls(list(plain_outcomes))
    client = SimpleNamespace(beta=SimpleNamespace(messages=beta), messages=plain)
    provider = AnthropicProvider("key", "claude-opus-5", client=client, **kwargs)
    return provider, beta, plain


def openai_provider(outcomes, **kwargs):
    calls = FakeCalls(list(outcomes))
    client = SimpleNamespace(chat=SimpleNamespace(completions=calls))
    return OpenAIProvider("key", "gpt-4o", client=client, **kwargs), calls


def bad_request(sdk, message: str):
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.test/v1"))
    return sdk.BadRequestError(message, response=response, body=None)


# --- shared schema preparation -------------------------------------------------

def test_strict_schema_requires_everything_and_forbids_extras() -> None:
    strict = strict_schema(SCHEMA)
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"root_cause", "confidence", "evidence"}
    nested = strict["properties"]["evidence"]["items"]
    assert nested["additionalProperties"] is False and nested["required"] == ["quote"]
    assert strict["properties"]["confidence"]["minimum"] == 0.0  # kept for Anthropic


def test_strict_schema_can_drop_unsupported_keywords() -> None:
    strict = strict_schema(SCHEMA, drop=("minimum", "maximum"))
    assert strict["properties"]["confidence"] == {"type": "number"}


# --- Anthropic ------------------------------------------------------------------

async def test_anthropic_sends_system_separately_and_parses_the_answer() -> None:
    provider, beta, plain = anthropic_provider([anthropic_reply()])
    result = await provider.complete_json(MESSAGES, SCHEMA)

    assert result.content == ANSWER
    assert (result.provider, result.model) == ("anthropic", "claude-opus-5")
    assert (result.prompt_tokens, result.completion_tokens) == (1200, 300)

    request = beta.calls[0]
    assert plain.calls == []  # the fallback-enabled endpoint was used
    assert request["betas"] == [REFUSAL_FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["system"] == "be careful"
    assert request["messages"] == [{"role": "user", "content": "why did it fail?"}]
    assert request["max_tokens"] == 16_000
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert "temperature" not in request  # not accepted by current Claude models


async def test_anthropic_without_refusal_fallback_uses_the_plain_endpoint() -> None:
    provider, beta, plain = anthropic_provider([], [anthropic_reply()], refusal_fallback=False)
    await provider.complete_json(MESSAGES, SCHEMA)
    assert beta.calls == [] and len(plain.calls) == 1


async def test_anthropic_falls_back_when_the_beta_is_not_enabled() -> None:
    provider, beta, plain = anthropic_provider(
        [bad_request(anthropic, "beta 'server-side-fallback' is not enabled")],
        [anthropic_reply(), anthropic_reply()],
    )
    await provider.complete_json(MESSAGES, SCHEMA)
    await provider.complete_json(MESSAGES, SCHEMA)

    assert len(beta.calls) == 1  # not attempted again
    assert len(plain.calls) == 2


async def test_anthropic_refusal_raises() -> None:
    provider, _, _ = anthropic_provider([anthropic_reply(stop_reason="refusal", category="cyber")])
    with pytest.raises(LLMError, match="declined to answer"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_anthropic_truncated_answer_raises() -> None:
    provider, _, _ = anthropic_provider([anthropic_reply(stop_reason="max_tokens")])
    with pytest.raises(LLMError, match="token limit"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_anthropic_invalid_json_raises() -> None:
    provider, _, _ = anthropic_provider([anthropic_reply("not json")])
    with pytest.raises(LLMError, match="valid JSON"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_anthropic_api_errors_become_llm_errors() -> None:
    error = anthropic.APIConnectionError(message="connection refused", request=httpx2.Request("POST", "https://api.test"))
    provider, _, _ = anthropic_provider([error])
    with pytest.raises(LLMError, match="Anthropic request failed"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_anthropic_other_bad_requests_are_not_swallowed() -> None:
    provider, _, _ = anthropic_provider([bad_request(anthropic, "model not found")])
    with pytest.raises(LLMError, match="model not found"):
        await provider.complete_json(MESSAGES, SCHEMA)


# --- OpenAI ---------------------------------------------------------------------

async def test_openai_sends_strict_schema_and_parses_the_answer() -> None:
    provider, calls = openai_provider([openai_reply()])
    result = await provider.complete_json(MESSAGES, SCHEMA)

    assert result.content == ANSWER
    assert (result.provider, result.model) == ("openai", "gpt-4o")
    assert (result.prompt_tokens, result.completion_tokens) == (1200, 300)

    payload = calls.calls[0]
    assert payload["messages"][0] == {"role": "system", "content": "be careful"}  # system stays a message
    assert payload["max_completion_tokens"] == 16_000 and "max_tokens" not in payload
    assert payload["temperature"] == 0.0
    schema_block = payload["response_format"]["json_schema"]
    assert schema_block["strict"] is True
    assert schema_block["schema"]["additionalProperties"] is False
    assert schema_block["schema"]["properties"]["confidence"] == {"type": "number"}  # bounds dropped


async def test_openai_compatible_server_uses_max_tokens() -> None:
    provider, calls = openai_provider([openai_reply()], base_url="http://vllm:8000/v1")
    await provider.complete_json(MESSAGES, SCHEMA)
    assert calls.calls[0]["max_tokens"] == 16_000 and "max_completion_tokens" not in calls.calls[0]


async def test_openai_retries_without_temperature_when_rejected() -> None:
    provider, calls = openai_provider(
        [bad_request(openai, "Unsupported value: 'temperature' does not support 0.0"), openai_reply(), openai_reply()]
    )
    await provider.complete_json(MESSAGES, SCHEMA)
    await provider.complete_json(MESSAGES, SCHEMA)

    assert "temperature" in calls.calls[0]
    assert "temperature" not in calls.calls[1]
    assert "temperature" not in calls.calls[2]  # remembered for later calls


async def test_openai_refusal_raises() -> None:
    provider, _ = openai_provider([openai_reply(refusal="I can't help with that")])
    with pytest.raises(LLMError, match="refused this request"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_openai_truncated_answer_raises() -> None:
    provider, _ = openai_provider([openai_reply(finish_reason="length")])
    with pytest.raises(LLMError, match="token limit"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_openai_api_errors_become_llm_errors() -> None:
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/v1"))
    provider, _ = openai_provider([error])
    with pytest.raises(LLMError, match="OpenAI request failed"):
        await provider.complete_json(MESSAGES, SCHEMA)

import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.llm.base import LLMError, LLMMessage
from app.llm.anthropic import AnthropicProvider
from app.llm.factory import build_llm_provider
from app.llm.openai import OpenAIProvider
from app.llm.ollama import OllamaProvider, is_cloud_model

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
MESSAGES = [LLMMessage("system", "be brief"), LLMMessage("user", "hi")]


async def instant_sleep(delay: float) -> None:
    """Stand-in for asyncio.sleep so retry tests do not wait."""


def ollama_with(handler) -> OllamaProvider:
    return OllamaProvider("http://ollama.test:11434", "qwen3:8b", keep_alive="2m", transport=httpx.MockTransport(handler))


async def test_ollama_sends_schema_and_parses_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"answer": "hello"}'}, "prompt_eval_count": 12, "eval_count": 5},
        )

    result = await ollama_with(handler).complete_json(MESSAGES, SCHEMA)

    assert result.content == {"answer": "hello"}
    assert (result.provider, result.model, result.prompt_tokens, result.completion_tokens) == ("ollama", "qwen3:8b", 12, 5)
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/chat"
    body = json.loads(request.content)
    assert body["format"] == SCHEMA
    assert body["stream"] is False
    assert body["keep_alive"] == "2m"
    assert body["options"] == {"temperature": 0.0, "num_ctx": 16384}
    assert body["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]


async def test_ollama_invalid_json_raises() -> None:
    provider = ollama_with(lambda r: httpx.Response(200, json={"message": {"content": "not json"}}))
    with pytest.raises(LLMError, match="valid JSON"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_ollama_non_object_json_raises() -> None:
    provider = ollama_with(lambda r: httpx.Response(200, json={"message": {"content": "[1, 2]"}}))
    with pytest.raises(LLMError, match="not an object"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_ollama_http_error_carries_message() -> None:
    provider = ollama_with(lambda r: httpx.Response(404, json={"error": "model 'qwen3:8b' not found"}))
    with pytest.raises(LLMError, match="404.*not found"):
        await provider.complete_json(MESSAGES, SCHEMA)


async def test_ollama_unreachable_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMError, match="ConnectError"):
        await ollama_with(handler).complete_json(MESSAGES, SCHEMA)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek-v4-flash:cloud", True),
        ("gpt-oss:120b-cloud", True),
        ("qwen3:8b", False),
        ("mistral", False),
        ("hf.co/ggml-org/gemma-4-31b-it-GGUF:Q4_K_M", False),
    ],
)
def test_is_cloud_model(model: str, expected: bool) -> None:
    assert is_cloud_model(model) is expected


def test_cloud_models_are_refused_unless_allowed() -> None:
    with pytest.raises(LLMError, match="cloud"):
        OllamaProvider("http://ollama.test", "glm-5.2:cloud")
    assert OllamaProvider("http://ollama.test", "glm-5.2:cloud", allow_cloud_models=True).model == "glm-5.2:cloud"


def test_factory_builds_ollama_from_settings() -> None:
    provider = build_llm_provider(Settings(_env_file=None, ollama_model="gemma3:1b"))
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "gemma3:1b"


def test_factory_builds_anthropic() -> None:
    provider = build_llm_provider(Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="sk-test"))
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-opus-5"


def test_factory_builds_openai() -> None:
    provider = build_llm_provider(Settings(_env_file=None, llm_provider="openai", openai_api_key="sk-test"))
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-4o"


def test_factory_builds_vllm_without_an_api_key() -> None:
    provider = build_llm_provider(
        Settings(_env_file=None, llm_provider="vllm", openai_base_url="http://vllm:8000/v1", openai_model="qwen")
    )
    assert isinstance(provider, OpenAIProvider) and provider.model == "qwen"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (Settings(_env_file=None, llm_provider="anthropic"), "ANTHROPIC_API_KEY"),
        (Settings(_env_file=None, llm_provider="openai"), "OPENAI_API_KEY"),
        (Settings(_env_file=None, llm_provider="vllm"), "OPENAI_BASE_URL"),
    ],
)
def test_factory_reports_missing_credentials(settings: Settings, message: str) -> None:
    with pytest.raises(LLMError, match=message):
        build_llm_provider(settings)


def test_llm_model_overrides_the_provider_model() -> None:
    settings = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="k", llm_model="claude-sonnet-5")
    assert build_llm_provider(settings).model == "claude-sonnet-5"
    assert settings.active_model == "claude-sonnet-5"


def test_active_model_follows_the_provider() -> None:
    assert Settings(_env_file=None).active_model.startswith("hf.co/ggml-org/gemma")
    assert Settings(_env_file=None, llm_provider="anthropic").active_model == "claude-opus-5"
    assert Settings(_env_file=None, llm_provider="openai").active_model == "gpt-4o"


def test_factory_unknown_provider() -> None:
    with pytest.raises(LLMError, match="Unknown"):
        build_llm_provider(Settings(_env_file=None, llm_provider="nope"))


async def test_oversized_prompt_is_refused_not_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an oversized prompt must not be sent")

    provider = OllamaProvider("http://ollama.test", "qwen3:8b", num_ctx=100, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="too large for the context window"):
        await provider.complete_json([LLMMessage("user", "x" * 1000)], SCHEMA)


def test_factory_passes_the_context_window() -> None:
    provider = build_llm_provider(Settings(_env_file=None, ollama_num_ctx=32768))
    assert provider._num_ctx == 32768


async def test_ollama_retries_connection_failures(monkeypatch) -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"message": {"content": '{"answer": "hi"}'}})

    monkeypatch.setattr("app.llm.ollama.asyncio", SimpleNamespace(sleep=instant_sleep))
    provider = OllamaProvider("http://ollama.test", "qwen3:8b", max_retries=2, transport=httpx.MockTransport(handler))
    result = await provider.complete_json(MESSAGES, SCHEMA)

    assert result.content == {"answer": "hi"} and len(attempts) == 2


async def test_ollama_gives_up_after_the_retry_limit(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("app.llm.ollama.asyncio", SimpleNamespace(sleep=instant_sleep))
    provider = OllamaProvider("http://ollama.test", "qwen3:8b", max_retries=1, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="unreachable"):
        await provider.complete_json(MESSAGES, SCHEMA)

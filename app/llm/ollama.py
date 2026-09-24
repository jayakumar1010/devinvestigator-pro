"""Ollama provider: local models via /api/chat with JSON-schema structured output."""

import asyncio
import json
import time
from typing import Any

import httpx

from app.llm.base import LLMError, LLMMessage, LLMResult


def is_cloud_model(model: str) -> bool:
    """Ollama marks models served from its cloud (not this machine) with a `cloud` tag."""
    tag = model.rpartition(":")[2] if ":" in model else ""
    return tag == "cloud" or tag.endswith("-cloud")


def _error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("error")
    except ValueError:
        message = None
    return str(message or response.reason_phrase)[:300]


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.0,
        timeout: float = 300.0,
        keep_alive: str = "2m",
        num_ctx: int = 16384,
        max_retries: int = 2,
        allow_cloud_models: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if is_cloud_model(model) and not allow_cloud_models:
            raise LLMError(
                f"Model {model!r} runs on Ollama's cloud, not locally, so evidence would leave this host. "
                "Choose a local model or set OLLAMA_ALLOW_CLOUD_MODELS=true."
            )
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._timeout = timeout
        self._keep_alive = keep_alive
        self._num_ctx = num_ctx
        self._max_retries = max_retries
        self._transport = transport

    def __repr__(self) -> str:
        return f"OllamaProvider(base_url={self._base_url!r}, model={self.model!r})"

    async def complete_json(self, messages: list[LLMMessage], schema: dict[str, Any]) -> LLMResult:
        # Ollama silently drops the start of a prompt that exceeds num_ctx; refuse instead.
        # ~3 characters per token is a conservative estimate for logs and code.
        estimated_tokens = (sum(len(m.content) for m in messages) + len(json.dumps(schema))) // 3
        if estimated_tokens > self._num_ctx:
            raise LLMError(
                f"Prompt is too large for the context window (~{estimated_tokens} tokens estimated, "
                f"num_ctx={self._num_ctx}); raise OLLAMA_NUM_CTX or reduce the evidence"
            )
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "format": schema,
            "stream": False,
            # Unload soon after use: the GPUs on this host are shared.
            "keep_alive": self._keep_alive,
            "options": {"temperature": self._temperature, "num_ctx": self._num_ctx},
        }
        started = time.monotonic()
        response = await self._post(payload)

        if response.status_code >= 400:
            raise LLMError(f"Ollama returned {response.status_code}: {_error_message(response)}")

        data = response.json()
        text = (data.get("message") or {}).get("content", "")
        try:
            content = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.model} did not return valid JSON") from exc
        if not isinstance(content, dict):
            raise LLMError(f"{self.model} returned JSON that is not an object")

        return LLMResult(
            content=content,
            raw_text=text,
            provider=self.name,
            model=self.model,
            duration_seconds=round(time.monotonic() - started, 2),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST /api/chat, retrying only connection failures (never a slow generation)."""
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url, timeout=self._timeout, transport=self._transport
                ) as http:
                    return await http.post("/api/chat", json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == self._max_retries:
                    raise LLMError(f"Ollama at {self._base_url} is unreachable: {type(exc).__name__}") from exc
                await asyncio.sleep(2**attempt)
            except httpx.HTTPError as exc:
                raise LLMError(f"Ollama request to {self._base_url} failed: {type(exc).__name__}") from exc
        raise LLMError(f"Ollama at {self._base_url} is unreachable")  # pragma: no cover

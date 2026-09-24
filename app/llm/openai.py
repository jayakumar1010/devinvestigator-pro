"""OpenAI provider, also used for any OpenAI-compatible server (vLLM, LiteLLM, ...).

The OpenAI SDK appears only in this file; the investigation code stays provider-neutral.
"""

import json
import time
from typing import Any

import openai
from openai import AsyncOpenAI

from app.llm.base import LLMError, LLMMessage, LLMResult
from app.llm.schema import strict_schema

# Keywords OpenAI's strict json_schema mode rejects. Pydantic still enforces them on the answer.
UNSUPPORTED_KEYWORDS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "pattern", "format", "default",
)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        max_tokens: int = 16_000,
        temperature: float | None = 0.0,
        timeout: float = 300.0,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # Self-hosted OpenAI-compatible servers expect max_tokens, OpenAI expects max_completion_tokens.
        self._compatible_server = base_url is not None
        if client is not None:
            self._client = client
        else:
            options: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
            if base_url:
                options["base_url"] = base_url
            self._client = AsyncOpenAI(**options)

    def __repr__(self) -> str:
        return f"OpenAIProvider(model={self.model!r})"

    async def complete_json(self, messages: list[LLMMessage], schema: dict[str, Any]) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "investigation_analysis",
                    "strict": True,
                    "schema": strict_schema(schema, drop=UNSUPPORTED_KEYWORDS),
                },
            },
        }
        payload["max_tokens" if self._compatible_server else "max_completion_tokens"] = self._max_tokens
        if self._temperature is not None:
            payload["temperature"] = self._temperature

        started = time.monotonic()
        response = await self._create(payload)
        duration = round(time.monotonic() - started, 2)

        choice = response.choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            raise LLMError(f"{self.model} refused this request: {message.refusal}")
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError(f"{self.model} reached the {self._max_tokens}-token limit before finishing the JSON")

        text = message.content or ""
        try:
            content = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.model} did not return valid JSON") from exc
        if not isinstance(content, dict):
            raise LLMError(f"{self.model} returned JSON that is not an object")

        usage = getattr(response, "usage", None)
        return LLMResult(
            content=content,
            raw_text=text,
            provider=self.name,
            model=getattr(response, "model", self.model),
            duration_seconds=duration,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )

    async def _create(self, payload: dict[str, Any]) -> Any:
        try:
            return await self._client.chat.completions.create(**payload)
        except openai.BadRequestError as exc:
            if self._temperature is None or "temperature" not in str(exc).lower():
                raise LLMError(f"OpenAI request failed: {exc}") from exc
            # Reasoning models reject a temperature; drop it for this and later calls.
            self._temperature = None
            payload.pop("temperature", None)
        except openai.APIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        try:
            return await self._client.chat.completions.create(**payload)
        except openai.APIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

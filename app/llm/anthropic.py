"""Anthropic (Claude API) provider, for deployments without a GPU.

The Anthropic SDK appears only in this file; app/analysis and app/agent stay
provider-neutral and talk to the LLMProvider interface in app/llm/base.py.
"""

import json
import time
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from app.llm.base import LLMError, LLMMessage, LLMResult
from app.llm.schema import strict_schema

# Server-side refusal fallbacks: if a safety classifier declines the request, the API
# re-runs it on a fallback model inside the same call instead of returning nothing.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 16_000,
        timeout: float = 300.0,
        max_retries: int = 2,
        refusal_fallback: bool = True,
        base_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._refusal_fallback = refusal_fallback
        if client is not None:
            self._client = client
        else:
            options: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
            if base_url:
                options["base_url"] = base_url
            self._client = AsyncAnthropic(**options)

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r})"

    async def complete_json(self, messages: list[LLMMessage], schema: dict[str, Any]) -> LLMResult:
        # Anthropic takes the system prompt as its own parameter, not as a message.
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        if not turns:
            raise LLMError("Anthropic needs at least one user message")

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": turns,
            "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)}},
        }
        if system:
            request["system"] = system

        started = time.monotonic()
        response = await self._create(request)
        duration = round(time.monotonic() - started, 2)

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", "unspecified")
            raise LLMError(f"Anthropic declined to answer (category: {category})")
        if stop_reason == "max_tokens":
            raise LLMError(f"{self.model} reached the {self._max_tokens}-token limit before finishing the JSON")

        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
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
            prompt_tokens=getattr(usage, "input_tokens", None),
            completion_tokens=getattr(usage, "output_tokens", None),
        )

    async def _create(self, request: dict[str, Any]) -> Any:
        if self._refusal_fallback:
            try:
                return await self._client.beta.messages.create(
                    betas=[REFUSAL_FALLBACK_BETA], fallbacks="default", **request
                )
            except anthropic.BadRequestError as exc:
                message = str(exc).lower()
                if "fallback" not in message and "beta" not in message:
                    raise LLMError(f"Anthropic request failed: {exc}") from exc
                # The account cannot use the fallback beta; carry on without it.
                self._refusal_fallback = False
            except anthropic.APIError as exc:
                raise LLMError(f"Anthropic request failed: {exc}") from exc
        try:
            return await self._client.messages.create(**request)
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc

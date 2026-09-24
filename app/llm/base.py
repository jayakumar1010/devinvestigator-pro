"""Provider-neutral LLM interface. The analysis layer depends only on this module."""

from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    """The LLM could not be reached or did not return usable output."""


@dataclass(frozen=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class LLMResult:
    content: dict[str, Any]  # the parsed JSON object
    raw_text: str
    provider: str
    model: str
    duration_seconds: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete_json(self, messages: list[LLMMessage], schema: dict[str, Any]) -> LLMResult:
        """Return a JSON object that conforms to `schema`."""
        ...

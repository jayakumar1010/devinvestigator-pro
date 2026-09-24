"""Prepare a JSON schema for providers that enforce it strictly.

Anthropic's `output_config.format` and OpenAI's strict `json_schema` both require every
object to list all its properties as required and to forbid extra ones.
"""

from collections.abc import Sequence
from typing import Any


def strict_schema(schema: dict[str, Any], drop: Sequence[str] = ()) -> dict[str, Any]:
    """Return the schema in strict form.

    `drop` removes validation keywords a provider rejects (OpenAI's strict mode has no
    numeric bounds, for example). Our own Pydantic validation still enforces them on the
    model's answer, so nothing is lost.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            cleaned = {key: walk(value) for key, value in node.items() if key not in drop}
            if cleaned.get("type") == "object" and "properties" in cleaned:
                cleaned["additionalProperties"] = False
                cleaned["required"] = list(cleaned["properties"])
            return cleaned
        if isinstance(node, list):
            return [walk(value) for value in node]
        return node

    return walk(schema)

"""Agent investigation (Phase 7): the model requests evidence through read-only tools, then answers.

Provider-neutral: every turn is a `complete_json` call with a JSON schema, so any LLMProvider
works without a native function-calling API. Tool results are appended as evidence (user)
messages, so the final answer's citations are validated against them exactly like the
initial evidence.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.agent.prompts import (
    FINAL_INSTRUCTION,
    LIMIT_REACHED,
    STEP_SCHEMA,
    AgentStep,
    build_agent_messages,
    render_tool_result,
)
from app.agent.tools import ToolResult
from app.analysis.analyzer import ANALYSIS_SCHEMA, build_result, parse_analysis
from app.analysis.models import AnalysisResult, LLMCallInfo, ToolCallRecord
from app.evidence.models import FailureEvidence
from app.llm.base import LLMError, LLMMessage, LLMProvider, LLMResult


class Tools(Protocol):
    async def run(self, tool: str, *, path: str = "") -> ToolResult: ...


@dataclass
class AgentRun:
    result: AnalysisResult
    messages: list[LLMMessage]  # the full transcript, for inspection


def _total(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


async def investigate(
    evidence: FailureEvidence,
    provider: LLMProvider,
    tools: Tools,
    *,
    max_tool_calls: int = 6,
    on_step: Callable[[ToolCallRecord], None] | None = None,
) -> AgentRun:
    messages = build_agent_messages(evidence)
    replies: list[LLMResult] = []
    records: list[ToolCallRecord] = []
    first_step_for: dict[tuple[str, str], int] = {}
    limit_reached = True

    for step in range(1, max_tool_calls + 1):
        reply = await provider.complete_json(messages, STEP_SCHEMA)
        replies.append(reply)
        try:
            decision = AgentStep.model_validate(reply.content)
        except ValidationError as exc:
            raise LLMError(
                f"{reply.provider}/{reply.model} returned an invalid tool step ({exc.error_count()} errors)"
            ) from exc
        messages.append(LLMMessage("assistant", reply.raw_text))
        if decision.action == "finish":
            limit_reached = False
            break

        started = time.monotonic()
        path = decision.path.strip() if decision.action == "get_file" else ""
        key = (decision.action, path)
        if key in first_step_for:
            arguments = {"path": path} if decision.action == "get_file" else {}
            result = ToolResult(
                decision.action, arguments, f"Already provided in step {first_step_for[key]}; see the result above.",
                ok=False, error="duplicate_request",
            )
        else:
            result = await tools.run(decision.action, path=path)
            first_step_for[key] = step

        record = ToolCallRecord(
            step=step,
            tool=decision.action,
            arguments=result.arguments,
            reasoning=decision.reasoning,
            ok=result.ok,
            error=result.error,
            result_chars=len(result.content),
            duration_seconds=round(time.monotonic() - started, 2),
        )
        records.append(record)
        if on_step is not None:
            on_step(record)
        messages.append(LLMMessage("user", render_tool_result(result)))

    messages.append(LLMMessage("user", f"{LIMIT_REACHED} {FINAL_INSTRUCTION}" if limit_reached else FINAL_INSTRUCTION))
    final = await provider.complete_json(messages, ANALYSIS_SCHEMA)
    replies.append(final)

    llm = LLMCallInfo(
        provider=final.provider,
        model=final.model,
        duration_seconds=round(sum(r.duration_seconds for r in replies), 2),
        prompt_tokens=_total([r.prompt_tokens for r in replies]),
        completion_tokens=_total([r.completion_tokens for r in replies]),
        calls=len(replies),
    )
    result = build_result(evidence, parse_analysis(final), messages, llm, mode="agent", tool_calls=records)
    return AgentRun(result=result, messages=messages)

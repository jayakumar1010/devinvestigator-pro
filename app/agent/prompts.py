"""Agent prompt and the per-turn step schema.

The agent's system prompt is the single-pass prompt with a Tools section added, so the
evidence, root cause, category, confidence and recommendation rules are identical.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools import ToolResult
from app.analysis.prompts import PROMPT_INTRO, SYSTEM_PROMPT, build_messages
from app.evidence.models import FailureEvidence
from app.llm.base import LLMMessage

AgentAction = Literal["get_file", "get_previous_successful_run", "compare_commits", "finish"]


class AgentStep(BaseModel):
    """What the model returns on each tool turn."""

    model_config = ConfigDict(extra="ignore")

    reasoning: str = Field(description="One or two sentences: what you need next and why, or why the evidence is sufficient.")
    action: AgentAction
    path: str = Field(description='Path for get_file, e.g. "lib/discount.js" or "" for the repository root; "" otherwise.')


STEP_SCHEMA = AgentStep.model_json_schema()

TOOLS_SECTION = """Tools:
- Before answering you may request more evidence, one tool per turn. Tools are read-only and limited to this repository and this failed run.
- get_file: read a file, or list a directory, at the failing commit. Set "path", for example "lib/discount.js", or "" for the repository root.
- get_previous_successful_run: find the most recent successful run of this workflow on this branch before the failure.
- compare_commits: list the commits and file changes between the last successful run's commit and the failing commit.
- finish: stop requesting evidence; you will then be asked for the final analysis.
- On a tool turn, reply with the tool step JSON. Request only what helps explain the failure, never the same thing twice, and choose finish as soon as the evidence explains the failure. Results appear in <tool_result> blocks."""

assert SYSTEM_PROMPT.startswith(PROMPT_INTRO)
AGENT_SYSTEM_PROMPT = PROMPT_INTRO + "\n\n" + TOOLS_SECTION + SYSTEM_PROMPT[len(PROMPT_INTRO):]

FINAL_INSTRUCTION = "Give your final analysis now as the analysis JSON object, based only on the evidence above."
LIMIT_REACHED = "The tool call limit has been reached; no more tools are available."


def build_agent_messages(evidence: FailureEvidence) -> list[LLMMessage]:
    _, evidence_message = build_messages(evidence)
    return [LLMMessage("system", AGENT_SYSTEM_PROMPT), evidence_message]


def render_tool_result(result: ToolResult) -> str:
    attributes = "".join(f' {key}="{value.replace(chr(34), chr(39))}"' for key, value in result.arguments.items())
    status = "ok" if result.ok else "error"
    # Keep repository content from closing its own block early.
    content = result.content.replace("</tool_result", "<\\/tool_result")
    return f'<tool_result tool="{result.tool}"{attributes} status="{status}">\n{content}\n</tool_result>'

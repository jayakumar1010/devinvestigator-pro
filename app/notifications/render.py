"""Render an investigation result as a GitHub comment (Markdown)."""

from app.analysis.models import AnalysisResult

MAX_BODY_CHARS = 60_000  # GitHub's limit is 65536
MAX_EVIDENCE_ITEMS = 6
MAX_QUOTE_CHARS = 400


def _fence(text: str) -> str:
    """Fence a quote so log characters cannot break the Markdown."""
    quote = text[:MAX_QUOTE_CHARS] + ("…" if len(text) > MAX_QUOTE_CHARS else "")
    fence = "`" * max(3, max((len(run) for run in quote.split("`") if run == ""), default=0) + 1)
    return f"{fence}text\n{quote}\n{fence}"


def render_comment(result: AnalysisResult, *, page_url: str | None = None) -> str:
    analysis = result.analysis
    confidence = f"{result.confidence:.2f}"
    basis = result.confidence_assessment.basis
    validation = result.evidence_validation
    verified = validation.checked - validation.invalid

    lines = [
        f"## 🔎 DevInvestigator: `{result.workflow or 'workflow'}` #{result.run_number} failed",
        "",
        f"**Failed stage:** `{result.failed_stage or 'unknown'}`",
        "",
        "### Root cause",
        analysis.root_cause,
        "",
        "### Suggested fix",
        analysis.recommendation,
        "",
        f"**Category:** `{analysis.category}` · **Confidence:** {confidence} ({basis}) · "
        f"**Evidence:** {verified}/{validation.checked} verified",
    ]

    if result.confidence_assessment.adjusted:
        lines += ["", f"> Confidence was capped: {result.confidence_assessment.reason}."]

    shown = [item for item in validation.items if item.valid][:MAX_EVIDENCE_ITEMS]
    if shown:
        lines += ["", "<details>", "<summary>Evidence (checked against the real logs and files)</summary>", ""]
        for item in shown:
            lines += [f"**{item.source}**", _fence(item.quote), ""]
        lines.append("</details>")

    if result.tool_calls:
        looked_at = ", ".join(
            f"`{call.tool}{'(' + call.arguments['path'] + ')' if call.arguments.get('path') else ''}`"
            for call in result.tool_calls if call.ok
        )
        if looked_at:
            lines += ["", f"The agent looked at: {looked_at}"]

    footer = []
    if result.run_url:
        footer.append(f"[Failed run]({result.run_url})")
    if page_url:
        footer.append(f"[Full investigation]({page_url})")
    footer.append(f"model `{result.llm.model}`")
    lines += ["", "---", "*Automated suggestion. DevInvestigator only reads your repository and never changes code.* "
              + " · ".join(footer)]

    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n…truncated."
    return body

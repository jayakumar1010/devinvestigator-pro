"""Turn a raw GitHub Actions job log into something worth reasoning over.

Raw logs carry ANSI colour codes and, after the failing step, GitHub's own cleanup
output (post-job hooks, deprecation warnings). The real error is marked with
`##[error]` and sits inside a `##[group]` section, so that is what we extract.
"""

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?", re.MULTILINE)
ERROR_MARKER = "##[error]"
GROUP_MARKER = "##[group]"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def strip_timestamps(text: str) -> str:
    """Drop the ISO timestamp GitHub prefixes to every log line (saves tokens)."""
    return TIMESTAMP_RE.sub("", text)


def extract_error_excerpt(
    text: str, *, context_before: int = 120, context_after: int = 2, max_chars: int = 8000
) -> str | None:
    """The failing section of a job log, or None if it carries no error marker.

    Anchors on the first `##[error]` line, rewinds to the start of the enclosing
    `##[group]` when there is one nearby, and keeps a little of what follows.
    """
    lines = text.splitlines()
    anchor = next((i for i, line in enumerate(lines) if ERROR_MARKER in line), None)
    if anchor is None:
        return None

    start = max(0, anchor - context_before)
    for i in range(anchor, start - 1, -1):
        if GROUP_MARKER in lines[i]:
            start = i
            break

    excerpt = "\n".join(lines[start : min(len(lines), anchor + context_after + 1)])
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt

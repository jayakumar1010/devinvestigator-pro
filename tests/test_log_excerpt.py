from app.evidence.logs import extract_error_excerpt, strip_ansi

# Shaped like a real GitHub Actions job log: timestamps, ANSI codes, group markers,
# and post-job cleanup after the failure.
REAL_SHAPE = """2026-09-21T09:24:39.0Z ##[group]Operating System
2026-09-21T09:24:39.1Z Ubuntu
2026-09-21T09:24:39.2Z ##[endgroup]
2026-09-21T09:24:40.0Z ##[group]Run actions/checkout@v4
2026-09-21T09:24:41.0Z Syncing repository
2026-09-21T09:24:41.5Z ##[endgroup]
2026-09-21T09:24:42.2Z ##[group]Run echo "npm ERR! code ERESOLVE"
2026-09-21T09:24:42.2Z \x1b[36;1mecho "npm ERR! code ERESOLVE"\x1b[0m
2026-09-21T09:24:42.2Z ##[endgroup]
2026-09-21T09:24:42.2Z npm ERR! code ERESOLVE
2026-09-21T09:24:42.2Z npm ERR! ERESOLVE unable to resolve dependency tree
2026-09-21T09:24:42.2Z npm ERR! Conflicting peer dependency: react@18.3.1
2026-09-21T09:24:42.2Z ##[error]Process completed with exit code 1.
2026-09-21T09:24:42.3Z Post job cleanup.
2026-09-21T09:24:42.4Z [command]/usr/bin/git config --local --unset-all http.extraheader
2026-09-21T09:24:42.4Z Cleaning up orphan processes
2026-09-21T09:24:42.4Z ##[warning]Node.js 20 is deprecated
"""


def test_strip_ansi() -> None:
    assert strip_ansi("\x1b[36;1mecho hi\x1b[0m") == "echo hi"
    assert strip_ansi("plain text") == "plain text"


def test_excerpt_captures_the_error_not_the_cleanup() -> None:
    excerpt = extract_error_excerpt(strip_ansi(REAL_SHAPE))

    assert excerpt is not None
    assert "npm ERR! code ERESOLVE" in excerpt
    assert "Conflicting peer dependency: react@18.3.1" in excerpt
    assert "##[error]Process completed with exit code 1." in excerpt
    # The failing step's own group header, not an earlier one.
    assert excerpt.startswith('2026-09-21T09:24:42.2Z ##[group]Run echo "npm ERR! code ERESOLVE"')
    assert "Operating System" not in excerpt
    assert "Syncing repository" not in excerpt
    # A little trailing context is fine, but not the whole cleanup tail.
    assert "Node.js 20 is deprecated" not in excerpt


def test_no_error_marker_returns_none() -> None:
    assert extract_error_excerpt("everything went fine\nbuild complete\n") is None


def test_excerpt_without_enclosing_group_uses_line_context() -> None:
    text = "\n".join([f"line {i}" for i in range(100)] + ["##[error]boom", "after"])
    excerpt = extract_error_excerpt(text, context_before=10, context_after=1)

    assert excerpt.startswith("line 90")
    assert excerpt.endswith("after")
    assert "line 89" not in excerpt


def test_excerpt_is_capped_and_keeps_the_error_end() -> None:
    noise = "\n".join(f"{i} " + "x" * 200 for i in range(200))
    excerpt = extract_error_excerpt(noise + "\n##[error]failed", context_before=500, max_chars=1000)

    assert len(excerpt) <= 1000
    assert excerpt.endswith("##[error]failed")

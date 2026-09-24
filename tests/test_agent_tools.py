import httpx
import pytest

from app.agent.tools import MAX_FILE_CHARS, InvestigationTools
from app.integrations.github.auth import TokenAuth
from app.integrations.github.client import GitHubClient
from tests.conftest import API_URL, FakeGitHub, load_fixture
from tests.test_analysis import make_evidence

REPO = f"{API_URL}/repos/acme/frontend"
FAILED_SHA = "abc123def4567890abc123def4567890abc123de"  # head_sha of the fixture run (run_number 182)
GOOD_SHA = "0000111122223333444455556666777788889999"
RUNS_URL = f"{REPO}/actions/workflows/123456/runs"
DISCOUNT_JS = (
    "// Returns the price after a percentage discount.\n"
    "function applyDiscount(price, percent) {\n"
    "  return price - percent;\n"
    "}\n"
)
COMPARISON = {
    "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
    "commits": [{"sha": FAILED_SHA, "commit": {"message": "Break the discount\n\nlonger body"}}],
    "files": [{
        "filename": "lib/discount.js", "status": "modified", "additions": 1, "deletions": 1,
        "patch": "@@ -2,3 +2,3 @@\n-  return price * (1 - percent / 100);\n+  return price - percent;",
    }],
}


def run_json(run_id: int, created_at: str, sha: str, number: int) -> dict:
    run = load_fixture("workflow_run_failed.json")["workflow_run"]
    return {
        **run, "id": run_id, "created_at": created_at, "head_sha": sha, "conclusion": "success",
        "run_number": number, "head_commit": {**run["head_commit"], "id": sha, "message": f"commit for run {number}"},
    }


def tools_for(fake: FakeGitHub, budget: int = 24_000) -> tuple[GitHubClient, InvestigationTools]:
    client = GitHubClient(API_URL, TokenAuth("t"), transport=fake.transport)
    return client, InvestigationTools(client, make_evidence(), budget_chars=budget)


async def test_get_file_returns_numbered_lines_at_the_failing_commit(fake_github: FakeGitHub) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == FAILED_SHA
        return httpx.Response(200, text=DISCOUNT_JS)

    fake_github.add("GET", f"{REPO}/contents/lib/discount.js", handler)
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_file", path="lib/discount.js")

    assert result.ok
    assert result.arguments == {"path": "lib/discount.js"}
    assert result.content.startswith(f"File lib/discount.js at commit {FAILED_SHA[:12]} (4 lines):")
    assert "   3 |   return price - percent;" in result.content


async def test_get_file_lists_the_repository_root(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/contents", httpx.Response(200, json=[
        {"type": "dir", "path": "lib"}, {"type": "file", "path": "package.json"},
    ]))
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_file", path="")

    assert result.content.startswith(f"Directory listing of / at commit {FAILED_SHA[:12]}:")
    assert "dir lib\nfile package.json" in result.content


@pytest.mark.parametrize("path", ["../secrets", "lib/../../etc/passwd", "a\\b"])
async def test_get_file_refuses_paths_outside_the_repository(fake_github: FakeGitHub, path: str) -> None:
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_file", path=path)

    assert (result.ok, result.error) == (False, "invalid_input")
    assert fake_github.requests == []


async def test_get_file_missing_file(fake_github: FakeGitHub) -> None:
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_file", path="nope.js")

    assert (result.ok, result.error) == (False, "not_found")
    assert f"No file or directory 'nope.js' exists at commit {FAILED_SHA[:12]}" in result.content


async def test_get_file_truncates_large_files(fake_github: FakeGitHub) -> None:
    big = "\n".join(f"line {i} " + "x" * 50 for i in range(1000))
    fake_github.add("GET", f"{REPO}/contents/big.txt", httpx.Response(200, text=big))
    client, tools = tools_for(fake_github, budget=100_000)
    async with client:
        result = await tools.run("get_file", path="big.txt")

    assert "[truncated: the file continues" in result.content
    assert len(result.content) < MAX_FILE_CHARS + 300


async def test_previous_successful_run_is_the_latest_one_before_the_failure(fake_github: FakeGitHub) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["branch"] == "main"
        assert request.url.params["status"] == "success"
        return httpx.Response(200, json={"workflow_runs": [
            run_json(300, "2026-09-16T08:00:00Z", "later0000", 190),  # after the failure: ignored
            run_json(111, "2026-09-14T09:00:00Z", GOOD_SHA, 181),
            run_json(110, "2026-09-10T09:00:00Z", "older0000", 180),
        ]})

    fake_github.add("GET", RUNS_URL, handler)
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_previous_successful_run")

    assert "run_number: 181" in result.content
    assert f"head_sha: {GOOD_SHA}" in result.content
    assert "commit message: commit for run 181" in result.content
    assert "190" not in result.content
    assert f"The failing run is run_number 182 at head_sha {FAILED_SHA}." in result.content


async def test_no_previous_successful_run(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", RUNS_URL, httpx.Response(200, json={"workflow_runs": []}))
    client, tools = tools_for(fake_github)
    async with client:
        previous = await tools.run("get_previous_successful_run")
        comparison = await tools.run("compare_commits")

    assert "No successful run of workflow 'CI' on branch 'main' was found" in previous.content
    assert comparison.content.startswith("Cannot compare")


async def test_compare_commits_between_last_success_and_failure(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", RUNS_URL, httpx.Response(200, json={"workflow_runs": [run_json(111, "2026-09-14T09:00:00Z", GOOD_SHA, 181)]}))
    fake_github.add("GET", f"{REPO}/compare/{GOOD_SHA}...{FAILED_SHA}", httpx.Response(200, json=COMPARISON))
    client, tools = tools_for(fake_github)
    async with client:
        await tools.run("get_previous_successful_run")
        result = await tools.run("compare_commits")

    assert result.content.startswith(f"Changes from {GOOD_SHA[:12]} (last successful run, run_number 181)")
    assert f"- {FAILED_SHA[:12]} Break the discount" in result.content
    assert "- modified lib/discount.js (+1/-1)" in result.content
    assert "-  return price * (1 - percent / 100);" in result.content
    assert "+  return price - percent;" in result.content
    assert sum(r.url.path.endswith("/runs") for r in fake_github.requests) == 1  # looked up once


async def test_compare_commits_same_commit(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", RUNS_URL, httpx.Response(200, json={"workflow_runs": [run_json(111, "2026-09-14T09:00:00Z", FAILED_SHA, 181)]}))
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("compare_commits")

    assert "used the same commit" in result.content
    assert not any("/compare/" in r.url.path for r in fake_github.requests)


async def test_budget_truncates_then_refuses(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/contents/lib/discount.js", httpx.Response(200, text=DISCOUNT_JS))
    client, tools = tools_for(fake_github, budget=60)
    async with client:
        first = await tools.run("get_file", path="lib/discount.js")
        second = await tools.run("get_previous_successful_run")

    assert first.ok and first.content.endswith("[truncated: evidence budget reached]")
    assert (second.ok, second.error) == (False, "budget_exhausted")


async def test_unknown_tool_is_refused_without_requests(fake_github: FakeGitHub) -> None:
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("delete_repository")

    assert (result.ok, result.error) == (False, "unknown_tool")
    assert fake_github.requests == []


async def test_github_errors_become_tool_errors(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", RUNS_URL, httpx.Response(500, json={"message": "Server Error"}))
    client, tools = tools_for(fake_github)
    async with client:
        result = await tools.run("get_previous_successful_run")

    assert (result.ok, result.error) == (False, "github_error")
    assert "The GitHub request failed" in result.content


async def test_tools_only_send_get_requests(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/contents/lib/discount.js", httpx.Response(200, text=DISCOUNT_JS))
    fake_github.add("GET", RUNS_URL, httpx.Response(200, json={"workflow_runs": [run_json(111, "2026-09-14T09:00:00Z", GOOD_SHA, 181)]}))
    fake_github.add("GET", f"{REPO}/compare/{GOOD_SHA}...{FAILED_SHA}", httpx.Response(200, json=COMPARISON))
    client, tools = tools_for(fake_github)
    async with client:
        for tool, path in [("get_file", "lib/discount.js"), ("get_previous_successful_run", ""), ("compare_commits", "")]:
            assert (await tools.run(tool, path=path)).ok

    assert {r.method for r in fake_github.requests} == {"GET"}

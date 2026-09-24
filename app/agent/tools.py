"""Read-only tools the investigation agent can call, each scoped to one failed run.

Every tool is a GET against the GitHub API through GitHubClient. The model chooses only
the tool and, for get_file, a path: repository, commit, workflow and branch come from the
failure evidence, so a tool can never reach outside the run being investigated.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.evidence.models import FailureEvidence
from app.integrations.github.client import GitHubClient
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.models import WorkflowRun

TOOL_NAMES = ("get_file", "get_previous_successful_run", "compare_commits")
MAX_PATH_CHARS = 300
MAX_FILE_CHARS = 12_000
MAX_COMPARE_CHARS = 15_000
MAX_PATCH_CHARS = 3_000
MAX_COMPARE_COMMITS = 30
SUCCESSFUL_RUNS_CHECKED = 20
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    arguments: dict[str, str]
    content: str
    ok: bool = True
    error: str | None = None


class ToolError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class InvestigationTools:
    def __init__(self, client: GitHubClient, evidence: FailureEvidence, *, budget_chars: int = 24_000) -> None:
        self._client = client
        self._evidence = evidence
        self._owner = evidence.repository.owner
        self._repo = evidence.repository.name
        self._budget = budget_chars
        self._used = 0
        self._previous_looked_up = False
        self._previous: WorkflowRun | None = None

    @property
    def chars_used(self) -> int:
        return self._used

    async def run(self, tool: str, *, path: str = "") -> ToolResult:
        arguments = {"path": path} if tool == "get_file" else {}
        if tool not in TOOL_NAMES:
            return ToolResult(tool, arguments, f"Unknown tool {tool!r}.", ok=False, error="unknown_tool")
        if self._used >= self._budget:
            return ToolResult(
                tool, arguments, "The evidence budget is used up; no more tool output is available.",
                ok=False, error="budget_exhausted",
            )
        try:
            if tool == "get_file":
                content = await self.get_file(path)
            elif tool == "get_previous_successful_run":
                content = await self.get_previous_successful_run()
            else:
                content = await self.compare_commits()
        except ToolError as exc:
            return ToolResult(tool, arguments, str(exc), ok=False, error=exc.code)
        except GitHubAPIError as exc:
            return ToolResult(tool, arguments, f"The GitHub request failed: {exc}", ok=False, error="github_error")

        remaining = self._budget - self._used
        if len(content) > remaining:
            content = content[:remaining] + "\n[truncated: evidence budget reached]"
        self._used += len(content)
        return ToolResult(tool, arguments, content)

    async def get_file(self, path: str) -> str:
        clean = path.strip().strip("/")
        if clean == ".":
            clean = ""
        if len(clean) > MAX_PATH_CHARS or "\\" in clean or ".." in clean.split("/"):
            raise ToolError("path must be a repository-relative path without '..'", "invalid_input")

        sha = self._evidence.run.head_sha
        where = f"{clean or '/'} at commit {sha[:12]}"
        try:
            content = await self._client.get_file(self._owner, self._repo, clean, ref=sha)
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                raise ToolError(f"No file or directory {clean or '/'!r} exists at commit {sha[:12]}.", "not_found") from exc
            raise

        if content.kind == "directory":
            return f"Directory listing of {where}:\n" + ("\n".join(content.entries) or "(empty)")
        if content.binary:
            return f"{where} is a binary file ({content.total_bytes} bytes); its content is not shown."
        lines = content.text.splitlines()
        body = "\n".join(f"{number:>4} | {line}" for number, line in enumerate(lines, 1))
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + f"\n[truncated: the file continues beyond {MAX_FILE_CHARS} characters]"
        return f"File {where} ({len(lines)} lines):\n{body}"

    async def _previous_success(self) -> WorkflowRun | None:
        """Most recent successful run of the same workflow and branch created before the failure."""
        if not self._previous_looked_up:
            failed = self._evidence.run
            runs = await self._client.list_workflow_runs(
                self._owner, self._repo, self._evidence.workflow.id,
                branch=failed.head_branch, status="success", per_page=SUCCESSFUL_RUNS_CHECKED,
            )
            earlier = [
                run for run in runs
                if run.id != failed.id
                and (failed.created_at is None or run.created_at is None or run.created_at < failed.created_at)
            ]
            self._previous = max(earlier, key=lambda run: run.created_at or _EARLIEST, default=None)
            self._previous_looked_up = True
        return self._previous

    async def get_previous_successful_run(self) -> str:
        failed = self._evidence.run
        workflow = self._evidence.workflow.name
        previous = await self._previous_success()
        if previous is None:
            return (
                f"No successful run of workflow {workflow!r} on branch {failed.head_branch!r} was found before "
                f"this failure (checked the {SUCCESSFUL_RUNS_CHECKED} most recent successful runs)."
            )
        lines = [
            f"Most recent successful run of workflow {workflow!r} on branch {failed.head_branch!r} before this failure:",
            f"run_number: {previous.run_number}",
            f"run_id: {previous.id}",
            f"head_sha: {previous.head_sha}",
            f"created_at: {previous.created_at.isoformat() if previous.created_at else 'unknown'}",
            f"event: {previous.event}",
        ]
        if previous.head_commit:
            lines.append(f"commit message: {previous.head_commit.message.splitlines()[0]}")
        lines.append(f"The failing run is run_number {failed.run_number} at head_sha {failed.head_sha}.")
        return "\n".join(lines)

    async def compare_commits(self) -> str:
        head = self._evidence.run.head_sha
        previous = await self._previous_success()
        if previous is None:
            return "Cannot compare: no successful run of this workflow on this branch was found before the failure."
        if previous.head_sha == head:
            return (
                f"The last successful run (run_number {previous.run_number}) used the same commit {head[:12]} as "
                "the failing run, so the failure is not explained by a code change."
            )

        comparison = await self._client.compare_commits(self._owner, self._repo, previous.head_sha, head)
        out = [
            f"Changes from {previous.head_sha[:12]} (last successful run, run_number {previous.run_number}) "
            f"to {head[:12]} (failing run):",
            f"status: {comparison.status}, {comparison.ahead_by} commit(s) ahead, {comparison.behind_by} behind",
            "",
            "Commits:",
        ]
        for commit in comparison.commits[:MAX_COMPARE_COMMITS]:
            out.append(f"- {commit.sha[:12]} {commit.commit.message.splitlines()[0] if commit.commit.message else ''}")
        if len(comparison.commits) > MAX_COMPARE_COMMITS:
            out.append(f"- ... and {len(comparison.commits) - MAX_COMPARE_COMMITS} more")
        out += ["", f"Files changed ({len(comparison.files)}):"]
        out += [f"- {f.status} {f.filename} (+{f.additions}/-{f.deletions})" for f in comparison.files]

        size = sum(len(line) + 1 for line in out)
        for changed in comparison.files:
            if not changed.patch:
                continue
            patch = changed.patch
            if len(patch) > MAX_PATCH_CHARS:
                patch = patch[:MAX_PATCH_CHARS] + "\n[diff truncated]"
            block = f"\n--- diff of {changed.filename} ---\n{patch}"
            if size + len(block) > MAX_COMPARE_CHARS:
                out.append("\n[remaining diffs omitted: size limit reached]")
                break
            out.append(block)
            size += len(block) + 1
        return "\n".join(out)

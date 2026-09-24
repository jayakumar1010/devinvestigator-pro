import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings
from app.integrations.github.auth import READ_ONLY_PERMISSIONS, AppAuth, TokenAuth, build_github_auth
from app.integrations.github.client import GitHubClient
from app.integrations.github.errors import GitHubAPIError
from tests.conftest import API_URL, GITHUB_TOKEN, FakeGitHub, load_fixture

REPO = f"{API_URL}/repos/acme/frontend"
RUN_ID = 9876543210
SIGNED_LOG_URL = "https://results-receiver.actions.githubusercontent.com/logs/2002?sig=SIGNED-SECRET"


def make_client(fake: FakeGitHub, auth=None, **kwargs) -> GitHubClient:
    return GitHubClient(API_URL, auth or TokenAuth(GITHUB_TOKEN), transport=fake.transport, **kwargs)


@pytest.fixture
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def test_get_repository_and_workflow_run(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", REPO, httpx.Response(200, json=load_fixture("workflow_run_failed.json")["repository"]))
    fake_github.add(
        "GET", f"{REPO}/actions/runs/{RUN_ID}",
        httpx.Response(200, json=load_fixture("workflow_run_failed.json")["workflow_run"]),
    )
    async with make_client(fake_github) as client:
        repository = await client.get_repository("acme", "frontend")
        run = await client.get_workflow_run("acme", "frontend", RUN_ID)

    assert repository.full_name == "acme/frontend"
    assert repository.default_branch == "main"
    assert run.conclusion == "failure"
    assert run.head_sha.startswith("abc123")

    request = fake_github.requests[0]
    assert request.headers["Authorization"] == f"Bearer {GITHUB_TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


async def test_get_workflow_jobs_for_attempt_paginates(fake_github: FakeGitHub) -> None:
    all_jobs = [
        {"id": i, "run_id": RUN_ID, "name": f"job-{i}", "conclusion": "failure" if i == 150 else "success"}
        for i in range(1, 151)
    ]

    def jobs_page(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        per_page = int(request.url.params["per_page"])
        chunk = all_jobs[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json={"total_count": len(all_jobs), "jobs": chunk})

    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}/attempts/2/jobs", jobs_page)
    async with make_client(fake_github) as client:
        jobs = await client.get_workflow_jobs("acme", "frontend", RUN_ID, attempt=2)
        failed = await client.get_failed_jobs("acme", "frontend", RUN_ID, attempt=2)

    assert len(jobs) == 150
    assert [job.id for job in failed] == [150]
    assert [r.url.params["page"] for r in fake_github.requests] == ["1", "2", "1", "2"]


async def test_get_workflow_jobs_latest_attempt_uses_filter(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}/jobs", httpx.Response(200, json=load_fixture("workflow_jobs.json")))
    async with make_client(fake_github) as client:
        jobs = await client.get_workflow_jobs("acme", "frontend", RUN_ID)

    assert [job.name for job in jobs] == ["test", "build"]
    assert fake_github.requests[0].url.params["filter"] == "latest"


async def test_get_job_logs_follows_redirect_without_sending_credentials(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": SIGNED_LOG_URL}))
    fake_github.add("GET", SIGNED_LOG_URL, httpx.Response(200, text="line 1\nnpm ERR! ERESOLVE\n"))

    async with make_client(fake_github) as client:
        log = await client.get_job_logs("acme", "frontend", 2002)

    assert log.content == "line 1\nnpm ERR! ERESOLVE\n"
    assert log.truncated is False
    api_request, download_request = fake_github.requests
    assert api_request.headers["Authorization"] == f"Bearer {GITHUB_TOKEN}"
    assert download_request.url.host == "results-receiver.actions.githubusercontent.com"
    assert "Authorization" not in download_request.headers


async def test_get_job_logs_keeps_tail_on_line_boundary(fake_github: FakeGitHub) -> None:
    lines = [f"line {i:03d}" for i in range(100)]
    body = "\n".join(lines) + "\n"
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": SIGNED_LOG_URL}))
    fake_github.add("GET", SIGNED_LOG_URL, httpx.Response(200, text=body))

    async with make_client(fake_github, log_max_bytes=50) as client:
        log = await client.get_job_logs("acme", "frontend", 2002)

    assert log.truncated is True
    assert log.total_bytes == len(body.encode())
    assert log.content.endswith("line 099\n")
    assert log.content.startswith("line ")
    assert len(log.content.encode()) <= 50


async def test_expired_job_logs_raise_api_error(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(410, json={"message": "Gone"}))
    async with make_client(fake_github) as client:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.get_job_logs("acme", "frontend", 2002)

    assert exc_info.value.status_code == 410
    assert "Gone" in str(exc_info.value)


async def test_log_download_errors_do_not_leak_signed_url(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": SIGNED_LOG_URL}))
    fake_github.add("GET", SIGNED_LOG_URL, httpx.Response(403, text="AuthenticationFailed"))

    async with make_client(fake_github) as client:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.get_job_logs("acme", "frontend", 2002)

    assert "SIGNED-SECRET" not in str(exc_info.value)


async def test_non_https_log_redirect_is_refused(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": "http://evil.example/log"}))
    async with make_client(fake_github) as client:
        with pytest.raises(GitHubAPIError):
            await client.get_job_logs("acme", "frontend", 2002)

    assert len(fake_github.requests) == 1


async def test_get_commit(fake_github: FakeGitHub) -> None:
    sha = "abc123def4567890abc123def4567890abc123de"
    fake_github.add("GET", f"{REPO}/commits/{sha}", httpx.Response(200, json=load_fixture("commit.json")))
    async with make_client(fake_github) as client:
        commit = await client.get_commit("acme", "frontend", sha)

    assert commit.commit.message == "Upgrade dependencies"
    assert [f.filename for f in commit.files] == ["package.json", "package-lock.json"]


async def test_path_segments_are_escaped(fake_github: FakeGitHub) -> None:
    async with make_client(fake_github) as client:
        with pytest.raises(GitHubAPIError):
            await client.get_commit("acme", "frontend", "../../../user")

    assert fake_github.requests[0].url.raw_path == b"/repos/acme/frontend/commits/..%2F..%2F..%2Fuser"


async def test_api_errors_carry_status_and_message(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", REPO, httpx.Response(403, json={"message": "API rate limit exceeded"}))
    async with make_client(fake_github) as client:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.get_repository("acme", "frontend")

    assert exc_info.value.status_code == 403
    assert "rate limit" in str(exc_info.value)
    assert GITHUB_TOKEN not in str(exc_info.value)


async def test_client_only_sends_get_requests(fake_github: FakeGitHub) -> None:
    sha = "abc123def4567890abc123def4567890abc123de"
    fake_github.add("GET", REPO, httpx.Response(200, json=load_fixture("workflow_run_failed.json")["repository"]))
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}", httpx.Response(200, json=load_fixture("workflow_run_failed.json")["workflow_run"]))
    fake_github.add("GET", f"{REPO}/actions/runs/{RUN_ID}/jobs", httpx.Response(200, json=load_fixture("workflow_jobs.json")))
    fake_github.add("GET", f"{REPO}/actions/jobs/2002/logs", httpx.Response(302, headers={"Location": SIGNED_LOG_URL}))
    fake_github.add("GET", SIGNED_LOG_URL, httpx.Response(200, text="log"))
    fake_github.add("GET", f"{REPO}/commits/{sha}", httpx.Response(200, json=load_fixture("commit.json")))

    async with make_client(fake_github) as client:
        await client.get_repository("acme", "frontend")
        await client.get_workflow_run("acme", "frontend", RUN_ID)
        await client.get_workflow_jobs("acme", "frontend", RUN_ID)
        await client.get_failed_jobs("acme", "frontend", RUN_ID)
        await client.get_job_logs("acme", "frontend", 2002)
        await client.get_commit("acme", "frontend", sha)

    assert {request.method for request in fake_github.requests} == {"GET"}


async def test_app_auth_mints_read_only_installation_token_and_caches_it(
    fake_github: FakeGitHub, rsa_private_key_pem: str
) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    fake_github.add(
        "POST", f"{API_URL}/app/installations/555/access_tokens",
        httpx.Response(201, json={"token": "installation-token", "expires_at": expires}),
    )
    fake_github.add("GET", REPO, httpx.Response(200, json=load_fixture("workflow_run_failed.json")["repository"]))

    auth = AppAuth("12345", rsa_private_key_pem)
    async with make_client(fake_github, auth=auth, installation_id=555) as client:
        await client.get_repository("acme", "frontend")
        await client.get_repository("acme", "frontend")

    token_requests = [r for r in fake_github.requests if r.method == "POST"]
    assert len(token_requests) == 1
    assert json.loads(token_requests[0].content) == {"permissions": READ_ONLY_PERMISSIONS}

    app_jwt = token_requests[0].headers["Authorization"].removeprefix("Bearer ")
    public_key = serialization.load_pem_private_key(rsa_private_key_pem.encode(), None).public_key()
    claims = jwt.decode(app_jwt, public_key, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - time.time() <= 600

    api_requests = [r for r in fake_github.requests if r.method == "GET"]
    assert all(r.headers["Authorization"] == "Bearer installation-token" for r in api_requests)


async def test_app_auth_requires_installation_id(fake_github: FakeGitHub, rsa_private_key_pem: str) -> None:
    async with make_client(fake_github, auth=AppAuth("12345", rsa_private_key_pem)) as client:
        with pytest.raises(GitHubAPIError, match="installation id"):
            await client.get_repository("acme", "frontend")
    assert fake_github.requests == []


def test_build_github_auth_selects_mode(rsa_private_key_pem: str) -> None:
    assert build_github_auth(Settings(_env_file=None)) is None

    token_auth = build_github_auth(Settings(_env_file=None, github_token="secret-token-value"))
    assert isinstance(token_auth, TokenAuth)
    assert "secret-token-value" not in repr(token_auth)

    escaped_key = rsa_private_key_pem.replace("\n", "\\n")
    app_auth = build_github_auth(
        Settings(_env_file=None, github_app_id="12345", github_app_private_key=escaped_key, github_token="ignored")
    )
    assert isinstance(app_auth, AppAuth)
    assert "PRIVATE KEY" not in repr(app_auth)

    with pytest.raises(ValueError):
        build_github_auth(Settings(_env_file=None, github_app_id="12345"))


def test_settings_repr_hides_secrets() -> None:
    settings = Settings(_env_file=None, github_token="secret-token-value", github_webhook_secret="hook-secret")
    assert "secret-token-value" not in repr(settings)
    assert "hook-secret" not in repr(settings)
    assert settings.github_auth_mode == "token"


async def test_get_file_returns_raw_content_at_ref(fake_github: FakeGitHub) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "abc123"
        assert request.headers["Accept"] == "application/vnd.github.raw+json"
        return httpx.Response(200, text="line one\nline two\n")

    fake_github.add("GET", f"{REPO}/contents/lib/discount.js", handler)
    async with make_client(fake_github) as client:
        content = await client.get_file("acme", "frontend", "lib/discount.js", ref="abc123")

    assert (content.kind, content.text, content.total_bytes, content.binary) == ("file", "line one\nline two\n", 18, False)


async def test_get_file_lists_directories(fake_github: FakeGitHub) -> None:
    listing = [{"type": "dir", "path": "lib", "name": "lib"}, {"type": "file", "path": "package.json", "name": "package.json"}]
    fake_github.add("GET", f"{REPO}/contents", httpx.Response(200, json=listing))
    async with make_client(fake_github) as client:
        content = await client.get_file("acme", "frontend", "", ref="abc123")

    assert (content.kind, content.entries) == ("directory", ("dir lib", "file package.json"))


async def test_get_file_escapes_path_segments_but_keeps_slashes(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/contents/src/my file.js", httpx.Response(200, text="x"))
    async with make_client(fake_github) as client:
        await client.get_file("acme", "frontend", "src/my file.js", ref="r")

    assert fake_github.requests[0].url.raw_path.startswith(b"/repos/acme/frontend/contents/src/my%20file.js")


async def test_get_file_detects_binary_content(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/contents/logo.png", httpx.Response(200, content=b"\x89PNG\r\n\x00\x01"))
    async with make_client(fake_github) as client:
        content = await client.get_file("acme", "frontend", "logo.png", ref="r")

    assert (content.binary, content.text) == (True, "")


async def test_list_workflow_runs_filters_by_branch_and_status(fake_github: FakeGitHub) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"per_page": "20", "branch": "main", "status": "success"}
        return httpx.Response(200, json={"total_count": 1, "workflow_runs": [load_fixture("workflow_run_failed.json")["workflow_run"]]})

    fake_github.add("GET", f"{REPO}/actions/workflows/123456/runs", handler)
    async with make_client(fake_github) as client:
        runs = await client.list_workflow_runs("acme", "frontend", 123456, branch="main", status="success")

    assert [run.id for run in runs] == [RUN_ID]


async def test_compare_commits(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/compare/aaa...bbb", httpx.Response(200, json={
        "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
        "commits": [{"sha": "bbb", "commit": {"message": "Break the discount"}}],
        "files": [{"filename": "lib/discount.js", "status": "modified", "additions": 1, "deletions": 1,
                   "patch": "@@ -1 +1 @@\n-a\n+b"}],
    }))
    async with make_client(fake_github) as client:
        comparison = await client.compare_commits("acme", "frontend", "aaa", "bbb")

    assert (comparison.status, comparison.ahead_by) == ("ahead", 1)
    assert comparison.commits[0].commit.message == "Break the discount"
    assert comparison.files[0].patch.startswith("@@")

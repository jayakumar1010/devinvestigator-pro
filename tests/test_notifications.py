"""GitHub comments: the only write DevInvestigator can perform."""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.analysis.analyzer import analyze_failure
from app.core.config import Settings
from app.database import repository
from app.database.session import create_engine, create_sessionmaker, init_db
from app.integrations.github.auth import TokenAuth
from app.integrations.github.client import GitHubClient
from app.notifications.github_comment import (
    GitHubCommenter,
    GitHubCommentError,
    page_url,
    post_investigation_comment,
)
from app.notifications.render import render_comment
from tests.conftest import API_URL, FakeGitHub, load_fixture
from tests.test_analysis import GOOD_ANALYSIS, FakeProvider, make_evidence
from tests.test_database import fields

REPO = f"{API_URL}/repos/acme/frontend"
SHA = "abc123def4567890abc123def4567890abc123de"


async def analysis_result(payload=GOOD_ANALYSIS):
    return await analyze_failure(make_evidence(), FakeProvider(payload))


def settings(**overrides) -> Settings:
    values = {"notify_github_comments": True, "github_comment_token": "write-token", **overrides}
    return Settings(_env_file=None, **values)


# --- the comment body ------------------------------------------------------------

async def test_comment_shows_the_answer_and_the_evidence() -> None:
    body = render_comment(await analysis_result(), page_url="https://devinv.example/investigations/7")

    assert GOOD_ANALYSIS["root_cause"] in body
    assert GOOD_ANALYSIS["recommendation"] in body
    assert "`dependency_failure`" in body and "0.90 (direct)" in body
    assert "2/2 verified" in body
    assert "npm ERR! Conflicting peer dependency: react@18.3.1" in body
    assert "[Full investigation](https://devinv.example/investigations/7)" in body
    assert "never changes code" in body


async def test_comment_explains_a_capped_confidence() -> None:
    invented = {**GOOD_ANALYSIS, "evidence": ["this line was never in the log"], "confidence": 0.95}
    body = render_comment(await analysis_result(invented))

    assert "Confidence was capped" in body
    assert "0.89" in body
    assert "this line was never in the log" not in body  # unverified quotes are not shown


async def test_long_quotes_are_shortened_and_fenced() -> None:
    long_quote = "npm ERR! " + "x" * 2000
    result = await analysis_result({**GOOD_ANALYSIS, "evidence": [long_quote]})
    # Force the quote to count as verified so it is rendered.
    result.evidence_validation.items[0].valid = True
    result.evidence_validation.invalid = 0
    body = render_comment(result)

    assert "…" in body and "```text" in body
    assert len(body) < 5000


def test_page_url_needs_a_public_base_url() -> None:
    assert page_url(settings(), 7) is None
    assert page_url(settings(public_base_url="https://x.example/"), 7) == "https://x.example/investigations/7"


# --- posting ---------------------------------------------------------------------

def commenter_for(fake: FakeGitHub) -> GitHubCommenter:
    return GitHubCommenter(API_URL, "write-token", transport=fake.transport)


async def test_pull_request_comment(fake_github: FakeGitHub) -> None:
    fake_github.add("POST", f"{REPO}/issues/42/comments", httpx.Response(201, json={"html_url": "https://github.com/c/1"}))
    async with commenter_for(fake_github) as commenter:
        url = await commenter.comment_on_pull_request("acme", "frontend", 42, "hello")

    assert url == "https://github.com/c/1"
    request = fake_github.requests[0]
    assert json.loads(request.content) == {"body": "hello"}
    assert request.headers["Authorization"] == "Bearer write-token"


async def test_commit_comment(fake_github: FakeGitHub) -> None:
    fake_github.add("POST", f"{REPO}/commits/{SHA}/comments", httpx.Response(201, json={"html_url": "https://github.com/c/2"}))
    async with commenter_for(fake_github) as commenter:
        assert await commenter.comment_on_commit("acme", "frontend", SHA, "hi") == "https://github.com/c/2"


async def test_comment_errors_are_reported(fake_github: FakeGitHub) -> None:
    fake_github.add("POST", f"{REPO}/issues/42/comments", httpx.Response(403, json={"message": "Resource not accessible by personal access token"}))
    async with commenter_for(fake_github) as commenter:
        with pytest.raises(GitHubCommentError, match="403"):
            await commenter.comment_on_pull_request("acme", "frontend", 42, "hello")


async def test_comments_on_the_pull_request_when_there_is_one(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/commits/{SHA}/pulls", httpx.Response(200, json=[{"number": 42, "state": "open"}]))
    fake_github.add("POST", f"{REPO}/issues/42/comments", httpx.Response(201, json={"html_url": "https://github.com/pr"}))

    async with GitHubClient(API_URL, TokenAuth("read"), transport=fake_github.transport) as client:
        url = await post_investigation_comment(
            client, settings(), 7, make_evidence(), await analysis_result(), transport=fake_github.transport
        )

    assert url == "https://github.com/pr"
    assert [(r.method, r.url.path) for r in fake_github.requests] == [
        ("GET", f"/repos/acme/frontend/commits/{SHA}/pulls"),
        ("POST", "/repos/acme/frontend/issues/42/comments"),
    ]


async def test_falls_back_to_a_commit_comment(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/commits/{SHA}/pulls", httpx.Response(200, json=[]))
    fake_github.add("POST", f"{REPO}/commits/{SHA}/comments", httpx.Response(201, json={"html_url": "https://github.com/commit"}))

    async with GitHubClient(API_URL, TokenAuth("read"), transport=fake_github.transport) as client:
        url = await post_investigation_comment(
            client, settings(), 7, make_evidence(), await analysis_result(), transport=fake_github.transport
        )
    assert url == "https://github.com/commit"


async def test_a_failed_pull_request_lookup_still_comments(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/commits/{SHA}/pulls", httpx.Response(500, json={"message": "Server Error"}))
    fake_github.add("POST", f"{REPO}/commits/{SHA}/comments", httpx.Response(201, json={"html_url": "https://github.com/commit"}))

    async with GitHubClient(API_URL, TokenAuth("read"), transport=fake_github.transport) as client:
        assert await post_investigation_comment(
            client, settings(), 7, make_evidence(), await analysis_result(), transport=fake_github.transport
        ) == "https://github.com/commit"


async def test_without_a_comment_token_it_refuses(fake_github: FakeGitHub) -> None:
    async with GitHubClient(API_URL, TokenAuth("read"), transport=fake_github.transport) as client:
        with pytest.raises(GitHubCommentError, match="GITHUB_COMMENT_TOKEN"):
            await post_investigation_comment(
                client, settings(github_comment_token=None), 7, make_evidence(), await analysis_result()
            )
    assert fake_github.requests == []


async def test_only_comment_endpoints_are_written(fake_github: FakeGitHub) -> None:
    fake_github.add("GET", f"{REPO}/commits/{SHA}/pulls", httpx.Response(200, json=[{"number": 42, "state": "open"}]))
    fake_github.add("POST", f"{REPO}/issues/42/comments", httpx.Response(201, json={"html_url": "u"}))

    async with GitHubClient(API_URL, TokenAuth("read"), transport=fake_github.transport) as client:
        await post_investigation_comment(
            client, settings(), 7, make_evidence(), await analysis_result(), transport=fake_github.transport
        )

    writes = [r for r in fake_github.requests if r.method != "GET"]
    assert all(r.method == "POST" and r.url.path.endswith("/comments") for r in writes)


# --- stored on the investigation --------------------------------------------------

async def test_notification_result_is_stored(sessionmaker) -> None:
    result = await analysis_result()
    async with sessionmaker() as session:
        investigation, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        await repository.complete(session, investigation.id, result)
        await repository.save_notification(session, investigation.id, url="https://github.com/c/1")

    async with sessionmaker() as session:
        stored = await repository.get(session, investigation.id)
    assert stored.notification_url == "https://github.com/c/1"
    assert stored.notification_error is None


async def test_a_database_created_before_these_columns_is_upgraded(tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/old.db"
    engine = create_engine(url)
    async with engine.begin() as connection:  # an older schema, without the notification columns
        await connection.execute(text(
            "CREATE TABLE investigations (id INTEGER PRIMARY KEY, status VARCHAR(16), trigger VARCHAR(16),"
            " provider VARCHAR(32), repository VARCHAR(255), run_id BIGINT, run_attempt INTEGER, created_at DATETIME)"
        ))
    await init_db(engine)  # must add the new columns, not fail

    async with engine.begin() as connection:
        columns = {row[1] for row in (await connection.exec_driver_sql("PRAGMA table_info(investigations)")).all()}
    await engine.dispose()
    assert {"notification_url", "notification_error", "confidence", "evidence"} <= columns

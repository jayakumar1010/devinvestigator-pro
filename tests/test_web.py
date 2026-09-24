import httpx
import pytest
from sqlalchemy import update

from app.agent.investigator import investigate
from app.analysis.analyzer import analyze_failure
from app.core.config import Settings, get_settings
from app.database import repository
from app.database.models import Investigation
from app.main import app
from tests.test_agent import PACKAGE_JSON, FakeTools, ScriptedProvider, step
from tests.test_analysis import GOOD_ANALYSIS, FakeProvider, make_evidence
from tests.test_database import fields

PASSWORD = "correct horse battery staple"


def client_for(auth=("admin", PASSWORD)) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", auth=auth)


@pytest.fixture
def dashboard(sessionmaker):
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, dashboard_password=PASSWORD)
    yield sessionmaker
    app.dependency_overrides.clear()
    del app.state.sessionmaker


async def seed(sessionmaker, result=None, run_id: int = 1, finish: bool = True) -> int:
    async with sessionmaker() as session:
        investigation, _ = await repository.create_investigation(session, fields(run_id))
        if finish:
            await repository.claim_next(session)
            await repository.save_evidence(session, investigation.id, make_evidence())
            result = result or await analyze_failure(make_evidence(), FakeProvider(GOOD_ANALYSIS))
            await repository.complete(session, investigation.id, result)
    return investigation.id


async def test_credentials_are_required(dashboard) -> None:
    async with client_for(auth=None) as client:
        response = await client.get("/investigations")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


async def test_wrong_password_is_rejected(dashboard) -> None:
    async with client_for(auth=("admin", "wrong")) as client:
        assert (await client.get("/investigations")).status_code == 401
    async with client_for(auth=("root", PASSWORD)) as client:
        assert (await client.get("/investigations")).status_code == 401


async def test_page_is_disabled_without_a_password(sessionmaker) -> None:
    app.state.sessionmaker = sessionmaker
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, dashboard_password=None)
    try:
        async with client_for() as client:
            response = await client.get("/investigations")
    finally:
        app.dependency_overrides.clear()
        del app.state.sessionmaker
    assert response.status_code == 503
    assert "DASHBOARD_PASSWORD" in response.text


async def test_list_shows_investigations_and_refreshes_while_busy(dashboard) -> None:
    completed = await seed(dashboard, run_id=1)
    queued = await seed(dashboard, run_id=2, finish=False)
    async with client_for() as client:
        response = await client.get("/investigations")

    assert response.status_code == 200
    page = response.text
    assert f'href="/investigations/{completed}"' in page and f'href="/investigations/{queued}"' in page
    assert "acme/frontend" in page and "dependency_failure" in page
    assert '<meta http-equiv="refresh"' in page  # something is still queued


async def test_status_filter(dashboard) -> None:
    completed = await seed(dashboard, run_id=1)
    queued = await seed(dashboard, run_id=2, finish=False)
    async with client_for() as client:
        page = (await client.get("/investigations?status=completed")).text
        everything = (await client.get("/investigations?status=nonsense")).text

    assert f'href="/investigations/{completed}"' in page
    assert f'href="/investigations/{queued}"' not in page
    assert f'href="/investigations/{queued}"' in everything  # unknown filter shows all


async def test_detail_shows_the_result(dashboard) -> None:
    investigation_id = await seed(dashboard)
    async with client_for() as client:
        response = await client.get(f"/investigations/{investigation_id}")

    assert response.status_code == 200
    page = response.text
    assert GOOD_ANALYSIS["root_cause"] in page
    assert GOOD_ANALYSIS["recommendation"] in page
    assert "Evidence cited" in page and "✓" in page
    assert "Error section of the log" in page and "npm ERR! Conflicting peer dependency" in page
    assert "Upgrade dependencies" in page and "modified package.json (+3/-3)" in page
    assert "2026-09-21T09:24" not in page  # log timestamps stripped for reading
    assert '<meta http-equiv="refresh"' not in page
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_detail_shows_what_the_agent_looked_at(dashboard) -> None:
    provider = ScriptedProvider([step("get_file", "web/package.json", "Check the pinned versions."), step("finish"), GOOD_ANALYSIS])
    agent_run = await investigate(make_evidence(), provider, FakeTools({("get_file", "web/package.json"): PACKAGE_JSON}))
    investigation_id = await seed(dashboard, result=agent_run.result)
    async with client_for() as client:
        page = (await client.get(f"/investigations/{investigation_id}")).text

    assert "What the agent looked at" in page
    assert "get_file(web/package.json)" in page
    assert "Check the pinned versions." in page


async def test_model_output_is_escaped(dashboard) -> None:
    hostile = {**GOOD_ANALYSIS, "root_cause": "<script>alert(1)</script>", "evidence": ["<img src=x onerror=alert(1)>"]}
    result = await analyze_failure(make_evidence(), FakeProvider(hostile))
    investigation_id = await seed(dashboard, result=result)
    async with client_for() as client:
        page = (await client.get(f"/investigations/{investigation_id}")).text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page


async def test_only_https_links_are_rendered(dashboard) -> None:
    investigation_id = await seed(dashboard)
    async with dashboard() as session:
        await session.execute(update(Investigation).values(run_url="javascript:alert(1)"))
        await session.commit()
    async with client_for() as client:
        page = (await client.get(f"/investigations/{investigation_id}")).text
    assert "javascript:" not in page


async def test_failed_investigation_shows_the_error(dashboard) -> None:
    async with dashboard() as session:
        investigation, _ = await repository.create_investigation(session, fields())
        await repository.claim_next(session)
        await repository.fail(session, investigation.id, "Ollama returned 404: model not found")
    async with client_for() as client:
        page = (await client.get(f"/investigations/{investigation.id}")).text
    assert "Investigation failed" in page and "model not found" in page
    assert f"python -m app.cli retry {investigation.id}" in page


async def test_results_stored_by_older_versions_still_render(dashboard) -> None:
    investigation_id = await seed(dashboard)
    async with dashboard() as session:
        stored = await repository.get(session, investigation_id)
        old = dict(stored.result)
        old["evidence_validation"] = {"status": "verified", "checked": 1, "invalid": 0,
                                      "items": [{"text": "npm ERR! code ERESOLVE", "valid": True, "reason": None}]}
        stored.result = old
        await session.commit()
    async with client_for() as client:
        response = await client.get(f"/investigations/{investigation_id}")
    assert response.status_code == 200
    assert "npm ERR! code ERESOLVE" in response.text


async def test_unknown_investigation_is_404(dashboard) -> None:
    async with client_for() as client:
        assert (await client.get("/investigations/999")).status_code == 404


async def test_api_docs_are_off_and_css_is_public(dashboard) -> None:
    async with client_for(auth=None) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
        css = await client.get("/static/app.css")
    assert css.status_code == 200 and "--accent" in css.text

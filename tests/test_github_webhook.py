import json
from collections.abc import Iterator

import pytest

from app.core.config import Settings, get_settings
from app.core.security import verify_github_signature
from app.integrations.github.models import WorkflowRunEvent
from app.main import app
from tests.conftest import WEBHOOK_SECRET, load_fixture, sign


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def submit(self, event, delivery_id):
        self.calls.append((event, delivery_id))
        return 1, len(self.calls) == 1


@pytest.fixture
def collected() -> Iterator[list[tuple]]:
    """Stand-in orchestrator, so webhook tests never reach GitHub or a database."""
    orchestrator = FakeOrchestrator()
    app.state.orchestrator = orchestrator
    yield orchestrator.calls
    del app.state.orchestrator


def post(client, payload: dict | bytes, event: str = "workflow_run", signature: str | None = "auto"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": "delivery-1", "Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhooks/github", content=body, headers=headers)


def test_signature_matches_github_documented_example() -> None:
    # Test vector from GitHub's "Validating webhook deliveries" documentation.
    signature = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert verify_github_signature("It's a Secret to Everybody", b"Hello, World!", signature)
    assert not verify_github_signature("wrong secret", b"Hello, World!", signature)
    assert not verify_github_signature("It's a Secret to Everybody", b"Hello, World!", None)
    assert not verify_github_signature("It's a Secret to Everybody", b"Hello, World!", "sha1=abc")


def test_failed_workflow_run_is_accepted_and_collection_scheduled(api_client, collected) -> None:
    response = post(api_client, load_fixture("workflow_run_failed.json"))

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "investigation_id": 1,
        "delivery_id": "delivery-1",
        "repository": "acme/frontend",
        "workflow_run_id": 9876543210,
        "run_attempt": 1,
        "conclusion": "failure",
    }
    assert len(collected) == 1
    event, delivery_id = collected[0]
    assert isinstance(event, WorkflowRunEvent)
    assert event.workflow_run.id == 9876543210
    assert event.installation is not None and event.installation.id == 555
    assert delivery_id == "delivery-1"


def test_invalid_signature_is_rejected(api_client, collected) -> None:
    response = post(api_client, load_fixture("workflow_run_failed.json"), signature="sha256=" + "0" * 64)
    assert response.status_code == 401
    assert collected == []


def test_missing_signature_is_rejected(api_client, collected) -> None:
    response = post(api_client, load_fixture("workflow_run_failed.json"), signature=None)
    assert response.status_code == 401
    assert collected == []


def test_signature_over_different_body_is_rejected(api_client, collected) -> None:
    payload = load_fixture("workflow_run_failed.json")
    body = json.dumps(payload).encode()
    tampered = body.replace(b'"failure"', b'"success"', 1)
    response = api_client.post(
        "/webhooks/github",
        content=tampered,
        headers={"X-GitHub-Event": "workflow_run", "X-Hub-Signature-256": sign(body)},
    )
    assert response.status_code == 401


def test_unconfigured_secret_rejects_all_deliveries(collected) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, github_webhook_secret=None)
    try:
        from fastapi.testclient import TestClient

        response = post(TestClient(app), load_fixture("workflow_run_failed.json"))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert collected == []


def test_ping_event(api_client, collected) -> None:
    response = post(api_client, {"zen": "Keep it logically awesome.", "hook_id": 1}, event="ping")
    assert response.status_code == 200
    assert response.json() == {"status": "pong"}


def test_unsupported_event_is_ignored(api_client, collected) -> None:
    response = post(api_client, {"ref": "refs/heads/main"}, event="push")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert collected == []


@pytest.mark.parametrize(
    ("action", "conclusion"),
    [("requested", None), ("in_progress", None), ("completed", "success"), ("completed", "cancelled")],
)
def test_non_failed_workflow_runs_are_ignored(api_client, collected, action, conclusion) -> None:
    payload = load_fixture("workflow_run_failed.json")
    payload["action"] = action
    payload["workflow_run"]["conclusion"] = conclusion
    response = post(api_client, payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert collected == []


def test_malformed_workflow_run_payload(api_client, collected) -> None:
    response = post(api_client, {"action": "completed", "workflow_run": {"id": "not-a-number"}})
    assert response.status_code == 422
    assert "not-a-number" not in response.text
    assert collected == []


def test_responses_never_contain_the_secret(api_client, collected) -> None:
    for signature in ("auto", None):
        response = post(api_client, load_fixture("workflow_run_failed.json"), signature=signature)
        assert WEBHOOK_SECRET not in response.text


def test_redelivery_of_the_same_run_is_a_duplicate(api_client, collected) -> None:
    payload = load_fixture("workflow_run_failed.json")
    assert post(api_client, payload).status_code == 202
    second = post(api_client, payload)
    assert second.status_code == 200
    assert (second.json()["status"], second.json()["investigation_id"]) == ("duplicate", 1)


def test_failed_run_without_a_running_worker_is_503(api_client) -> None:
    assert post(api_client, load_fixture("workflow_run_failed.json")).status_code == 503

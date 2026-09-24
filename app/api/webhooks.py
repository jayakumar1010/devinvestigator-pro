import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.security import verify_github_signature
from app.integrations.github.events import is_failed_workflow_run
from app.integrations.github.models import WorkflowRunEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github")
async def github_webhook(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    if settings.github_webhook_secret is None:
        logger.error("Rejected GitHub webhook: GITHUB_WEBHOOK_SECRET is not configured")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub webhook secret is not configured")

    body = await request.body()
    if not verify_github_signature(settings.github_webhook_secret.get_secret_value(), body, x_hub_signature_256):
        logger.warning("Rejected GitHub webhook delivery %s: invalid signature", x_github_delivery)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "workflow_run":
        return {"status": "ignored", "reason": f"unsupported event: {x_github_event}"}

    try:
        event = WorkflowRunEvent.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid workflow_run payload") from None

    run = event.workflow_run
    if not is_failed_workflow_run(event):
        return {"status": "ignored", "reason": f"action={event.action} conclusion={run.conclusion}"}

    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Investigation worker is not running")
    investigation_id, created = await orchestrator.submit(event, x_github_delivery)

    details = {
        "investigation_id": investigation_id,
        "delivery_id": x_github_delivery,
        "repository": event.repository.full_name,
        "workflow_run_id": run.id,
        "run_attempt": run.run_attempt,
        "conclusion": run.conclusion,
    }
    if not created:
        logger.info("Duplicate delivery %s for investigation %s", x_github_delivery, investigation_id)
        return {"status": "duplicate", **details}

    logger.info(
        "Failed workflow run %s (attempt %s) in %s, delivery %s: queued as investigation %s",
        run.id, run.run_attempt, event.repository.full_name, x_github_delivery, investigation_id,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return {"status": "accepted", **details}

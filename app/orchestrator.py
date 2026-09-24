"""Investigation orchestrator: turns failed CI runs into stored investigations.

The database is the queue. A webhook (or `app.cli queue`) inserts an investigation with
status "queued"; one background worker claims the oldest queued row, collects evidence,
runs the analysis and stores the result. Investigations interrupted by a restart are
queued again at startup. One investigation runs at a time, so a shared GPU is never
asked for two analyses at once.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.investigator import investigate
from app.agent.tools import InvestigationTools
from app.analysis.analyzer import analyze_failure
from app.core.config import Settings
from app.database import repository
from app.evidence.collector import collect_evidence_for_run
from app.integrations.github.client import GitHubClient
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.models import WorkflowRunEvent
from app.llm.base import LLMError, LLMProvider
from app.llm.factory import build_llm_provider
from app.notifications.github_comment import post_investigation_comment

logger = logging.getLogger(__name__)


class InvestigationOrchestrator:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        client_factory: Callable[..., GitHubClient] = GitHubClient.from_settings,
        provider_factory: Callable[[Settings], LLMProvider] = build_llm_provider,
        notifier: Callable[..., Awaitable[str]] = post_investigation_comment,
    ) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._client_factory = client_factory
        self._provider_factory = provider_factory
        self._notifier = notifier
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        async with self._sessionmaker() as session:
            requeued = await repository.reset_interrupted(session)
        if requeued:
            logger.warning("Re-queued %d investigation(s) interrupted by a restart", requeued)
        self._task = asyncio.create_task(self._worker(), name="investigation-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def submit(self, event: WorkflowRunEvent, delivery_id: str | None) -> tuple[int, bool]:
        """Queue a failed run from a webhook. Returns (investigation id, created)."""
        fields = repository.run_fields(
            event.repository.full_name,
            event.workflow_run,
            installation_id=event.installation.id if event.installation else None,
            delivery_id=delivery_id,
            trigger="webhook",
        )
        async with self._sessionmaker() as session:
            investigation, created = await repository.create_investigation(session, fields)
            investigation_id = investigation.id
        if created:
            self._wakeup.set()
        return investigation_id, created

    async def _worker(self) -> None:
        while True:
            self._wakeup.clear()
            try:
                async with self._sessionmaker() as session:
                    investigation_id = await repository.claim_next(session)
            except Exception:
                logger.exception("Could not read the investigation queue")
                investigation_id = None
            if investigation_id is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), self._settings.worker_poll_seconds)
                continue
            try:
                await self.process(investigation_id)
            except Exception:  # never let one investigation stop the worker
                logger.exception("Investigation %s could not be recorded", investigation_id)

    async def process(self, investigation_id: int) -> None:
        """Run one claimed (status "running") investigation to completion or failure."""
        async with self._sessionmaker() as session:
            investigation = await repository.get(session, investigation_id)
            if investigation is None:
                return
            full_name = investigation.repository
            run_id, installation_id = investigation.run_id, investigation.installation_id
        owner, _, repo = full_name.partition("/")
        logger.info("Investigation %s started: %s run %s", investigation_id, full_name, run_id)

        timeout = self._settings.investigation_timeout_seconds
        try:
            await asyncio.wait_for(self._run(investigation_id, owner, repo, run_id, installation_id), timeout)
        except TimeoutError:
            await self._fail(investigation_id, f"timed out after {timeout:.0f} seconds")
        except (GitHubAPIError, LLMError, ValueError) as exc:
            await self._fail(investigation_id, str(exc))
        except Exception as exc:
            logger.exception("Investigation %s failed unexpectedly", investigation_id)
            await self._fail(investigation_id, f"unexpected error: {type(exc).__name__}")

    async def _run(self, investigation_id: int, owner: str, repo: str, run_id: int, installation_id: int | None) -> None:
        settings = self._settings
        async with self._client_factory(settings, installation_id=installation_id) as client:
            evidence = await collect_evidence_for_run(
                client, owner, repo, run_id, max_job_logs=settings.github_max_failed_job_logs
            )
            async with self._sessionmaker() as session:
                await repository.save_evidence(session, investigation_id, evidence)

            if not settings.auto_analyze:
                async with self._sessionmaker() as session:
                    await repository.mark_collected(session, investigation_id)
                logger.info("Investigation %s: evidence stored, analysis disabled (AUTO_ANALYZE=false)", investigation_id)
                return

            provider = self._provider_factory(settings)
            if settings.analysis_mode == "agent":
                tools = InvestigationTools(client, evidence, budget_chars=settings.agent_tool_budget_chars)
                agent_run = await investigate(evidence, provider, tools, max_tool_calls=settings.agent_max_tool_calls)
                result = agent_run.result
            else:
                result = await analyze_failure(evidence, provider)

            async with self._sessionmaker() as session:
                await repository.complete(session, investigation_id, result)
            logger.info(
                "Investigation %s completed: %s, confidence %.2f, evidence %s, %d tool call(s)",
                investigation_id, result.analysis.category, result.confidence,
                result.evidence_validation.status, len(result.tool_calls),
            )
            # The result is stored first: a notification problem must never lose an investigation.
            await self._notify(client, investigation_id, evidence, result)

    async def _notify(self, client: GitHubClient, investigation_id: int, evidence, result) -> None:
        if not self._settings.notify_github_comments:
            return
        try:
            url = await self._notifier(client, self._settings, investigation_id, evidence, result)
        except Exception as exc:
            logger.warning("Investigation %s: could not post the GitHub comment: %s", investigation_id, exc)
            async with self._sessionmaker() as session:
                await repository.save_notification(session, investigation_id, error=str(exc))
            return
        logger.info("Investigation %s: posted a comment at %s", investigation_id, url)
        async with self._sessionmaker() as session:
            await repository.save_notification(session, investigation_id, url=url)

    async def _fail(self, investigation_id: int, error: str) -> None:
        logger.warning("Investigation %s failed: %s", investigation_id, error)
        async with self._sessionmaker() as session:
            await repository.fail(session, investigation_id, error)

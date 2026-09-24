"""Manual investigation commands. LLM analysis is triggered by hand for now.

    python -m app.cli preview OWNER/REPO RUN_ID       # print exactly what the LLM would receive
    python -m app.cli analyze OWNER/REPO RUN_ID       # single-pass analysis of the collected evidence
    python -m app.cli investigate OWNER/REPO RUN_ID   # agent: may read files, find the last successful
                                                      # run and compare commits (read-only tools)

Stored investigations (the server investigates failed runs automatically):

    python -m app.cli list                            # recent investigations
    python -m app.cli show ID [--evidence]            # one investigation with its result
    python -m app.cli queue OWNER/REPO RUN_ID         # have the server investigate an existing run
    python -m app.cli retry ID                        # investigate a stored run again
    python -m app.cli comment-preview ID              # the GitHub comment for a result, without posting

In Docker: docker exec devinvestigator python -m app.cli investigate OWNER/REPO RUN_ID
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable
from typing import TextIO

from app.agent.investigator import AgentRun, investigate
from app.agent.tools import InvestigationTools
from app.analysis.analyzer import ANALYSIS_SCHEMA, analyze_failure
from app.analysis.models import AnalysisResult, ToolCallRecord
from app.analysis.prompts import build_messages
from app.core.config import Settings, get_settings
from app.database import repository
from app.database.models import Investigation
from app.database.session import create_engine, create_sessionmaker, init_db
from app.evidence.collector import collect_evidence_for_run
from app.evidence.models import FailureEvidence
from app.integrations.github.client import GitHubClient
from app.integrations.github.errors import GitHubAPIError
from app.integrations.github.events import FAILED_RUN_CONCLUSIONS
from app.integrations.github.models import WorkflowRun
from app.llm.base import LLMError, LLMMessage
from app.llm.factory import build_llm_provider
from app.notifications.github_comment import page_url
from app.notifications.render import render_comment


def _parse_repo(value: str) -> tuple[str, str]:
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError("expected OWNER/REPO")
    return owner, name


def _print_messages(messages: list[LLMMessage], out: TextIO) -> None:
    for message in messages:
        print(f"===== {message.role.upper()} MESSAGE ({len(message.content)} chars) =====", file=out)
        print(message.content, file=out)
    total = sum(len(m.content) for m in messages)
    print(f"===== total {total} chars (~{total // 4} tokens), plus the output schema =====", file=out)


def _warn_if_not_failed(evidence: FailureEvidence) -> None:
    if evidence.run.conclusion not in FAILED_RUN_CONCLUSIONS:
        print(f"warning: run conclusion is {evidence.run.conclusion!r}, not a failure", file=sys.stderr)


def _warn_on_validation(result: AnalysisResult) -> None:
    validation = result.evidence_validation
    if validation.status != "verified":
        print(
            f"warning: evidence validation {validation.status}: "
            f"{validation.invalid} of {validation.checked} cited items not found in the evidence sent",
            file=sys.stderr,
        )


def _print_step(record: ToolCallRecord) -> None:
    target = f" {record.arguments['path']!r}" if "path" in record.arguments else ""
    outcome = "ok" if record.ok else f"error: {record.error}"
    print(
        f"step {record.step}: {record.tool}{target} -> {outcome}, {record.result_chars} chars ({record.reasoning})",
        file=sys.stderr,
    )


async def _with_db(settings: Settings, action):
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        await init_db(engine)
        async with create_sessionmaker(engine)() as session:
            return await action(session)
    finally:
        await engine.dispose()


def _print_investigations(investigations: list[Investigation]) -> None:
    print(f"{'ID':>4}  {'STATUS':<10} {'CREATED (UTC)':<16}  {'RUN':<48} {'CATEGORY':<22} {'CONF':>4}")
    for inv in investigations:
        run = f"{inv.repository} / {inv.workflow or '?'} #{inv.run_number or '?'}"
        created = inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "-"
        confidence = f"{inv.confidence:.2f}" if inv.confidence is not None else "-"
        print(f"{inv.id:>4}  {inv.status:<10} {created:<16}  {run[:48]:<48} {(inv.category or '-'):<22} {confidence:>4}")


def _investigation_details(investigation: Investigation, include_evidence: bool) -> dict:
    details = {
        column.name: getattr(investigation, column.name)
        for column in Investigation.__table__.columns
        if column.name not in ("evidence", "result")
    }
    details["result"] = investigation.result
    if include_evidence:
        details["evidence"] = investigation.evidence
    return details


async def _database_command(settings: Settings, args: argparse.Namespace) -> int:
    async def action(session) -> int:
        if args.command == "list":
            _print_investigations(await repository.list_recent(session, args.limit))
            return 0
        investigation = await repository.get(session, args.id)
        if investigation is None:
            print(f"error: investigation {args.id} not found", file=sys.stderr)
            return 1
        if args.command == "comment-preview":
            if not investigation.result:
                print(f"error: investigation {args.id} has no result yet (status {investigation.status})", file=sys.stderr)
                return 1
            result = AnalysisResult.model_validate(investigation.result)
            print(render_comment(result, page_url=page_url(settings, investigation.id)))
            return 0
        if args.command == "show":
            print(json.dumps(_investigation_details(investigation, args.evidence), indent=2, default=str))
            return 0
        status = investigation.status
        if not await repository.requeue(session, args.id):
            print(
                f"error: investigation {args.id} is {status}; only failed, completed or collected "
                "investigations can be retried",
                file=sys.stderr,
            )
            return 1
        print(f"Investigation {args.id} queued again; the server picks it up within "
              f"{settings.worker_poll_seconds:.0f} seconds.")
        return 0

    return await _with_db(settings, action)


async def _fetch_run(settings: Settings, owner: str, repo: str, run_id: int) -> WorkflowRun:
    async with GitHubClient.from_settings(settings) as client:
        return await client.get_workflow_run(owner, repo, run_id)


async def _queue(settings: Settings, owner: str, repo: str, run_id: int) -> int:
    try:
        run = await _fetch_run(settings, owner, repo, run_id)
    except GitHubAPIError as exc:
        print(f"error: could not look up the run: {exc}", file=sys.stderr)
        return 2
    fields = repository.run_fields(f"{owner}/{repo}", run, installation_id=None, delivery_id=None, trigger="manual")

    async def action(session):
        return await repository.create_investigation(session, fields)

    investigation, created = await _with_db(settings, action)
    if created:
        print(f"Queued investigation {investigation.id} for {owner}/{repo} run {run.id} (attempt {run.run_attempt}). "
              f"The server picks it up within {settings.worker_poll_seconds:.0f} seconds; "
              f"follow it with: python -m app.cli show {investigation.id}")
    else:
        print(f"Run {run.id} (attempt {run.run_attempt}) already has investigation {investigation.id} "
              f"(status {investigation.status}). To run it again: python -m app.cli retry {investigation.id}")
    return 0


async def _collect(settings: Settings, owner: str, repo: str, run_id: int) -> FailureEvidence:
    async with GitHubClient.from_settings(settings) as client:
        return await collect_evidence_for_run(
            client, owner, repo, run_id, max_job_logs=settings.github_max_failed_job_logs
        )


async def _run_agent(
    settings: Settings,
    owner: str,
    repo: str,
    run_id: int,
    max_tool_calls: int,
    on_step: Callable[[ToolCallRecord], None],
) -> AgentRun:
    provider = build_llm_provider(settings)  # fail fast on configuration errors
    async with GitHubClient.from_settings(settings) as client:
        evidence = await collect_evidence_for_run(
            client, owner, repo, run_id, max_job_logs=settings.github_max_failed_job_logs
        )
        _warn_if_not_failed(evidence)
        tools = InvestigationTools(client, evidence, budget_chars=settings.agent_tool_budget_chars)
        return await investigate(evidence, provider, tools, max_tool_calls=max_tool_calls, on_step=on_step)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if getattr(args, "model", None):
        settings = settings.model_copy(update={"llm_model": args.model})

    if args.command in ("list", "show", "retry", "comment-preview"):
        return await _database_command(settings, args)
    owner, repo = args.repo
    if args.command == "queue":
        return await _queue(settings, owner, repo, args.run_id)

    if args.command == "investigate":
        max_calls = args.max_tool_calls if args.max_tool_calls is not None else settings.agent_max_tool_calls
        try:
            agent_run = await _run_agent(settings, owner, repo, args.run_id, max_calls, _print_step)
        except GitHubAPIError as exc:
            print(f"error: could not collect evidence: {exc}", file=sys.stderr)
            return 2
        except LLMError as exc:
            print(f"error: investigation failed: {exc}", file=sys.stderr)
            return 3
        if args.show_transcript:
            _print_messages(agent_run.messages, sys.stderr)
        _warn_on_validation(agent_run.result)
        print(agent_run.result.model_dump_json(indent=2))
        return 0

    try:
        evidence = await _collect(settings, owner, repo, args.run_id)
    except GitHubAPIError as exc:
        print(f"error: could not collect evidence: {exc}", file=sys.stderr)
        return 2
    _warn_if_not_failed(evidence)

    if args.command == "preview":
        _print_messages(build_messages(evidence), sys.stdout)
        if args.schema:
            print(json.dumps(ANALYSIS_SCHEMA, indent=2))
        return 0

    if args.show_prompt:
        _print_messages(build_messages(evidence), sys.stderr)
    try:
        result = await analyze_failure(evidence, build_llm_provider(settings))
    except LLMError as exc:
        print(f"error: analysis failed: {exc}", file=sys.stderr)
        return 3
    _warn_on_validation(result)
    print(result.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Manual DevInvestigator investigations")
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="print exactly what would be sent to the LLM; no LLM call")
    analyze = commands.add_parser("analyze", help="single-pass LLM analysis of the collected evidence")
    agent = commands.add_parser(
        "investigate", help="agent investigation with read-only tools (files, last successful run, commit comparison)"
    )
    for command in (preview, analyze, agent):
        command.add_argument("repo", type=_parse_repo, metavar="OWNER/REPO")
        command.add_argument("run_id", type=int, help="workflow run id (from the run URL)")
    for command in (analyze, agent):
        command.add_argument("--model", help="override LLM_MODEL for this run")
    preview.add_argument("--schema", action="store_true", help="also print the JSON output schema")
    analyze.add_argument("--show-prompt", action="store_true", help="print the prompt to stderr first")
    agent.add_argument("--max-tool-calls", type=int, help="override AGENT_MAX_TOOL_CALLS")
    agent.add_argument("--show-transcript", action="store_true", help="print the full conversation to stderr")

    listing = commands.add_parser("list", help="recent investigations stored by the server")
    listing.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="one stored investigation with its result")
    show.add_argument("id", type=int)
    show.add_argument("--evidence", action="store_true", help="include the collected evidence")
    queue = commands.add_parser("queue", help="have the server investigate an existing run")
    queue.add_argument("repo", type=_parse_repo, metavar="OWNER/REPO")
    queue.add_argument("run_id", type=int, help="workflow run id (from the run URL)")
    retry = commands.add_parser("retry", help="investigate a stored run again")
    retry.add_argument("id", type=int)
    preview = commands.add_parser("comment-preview", help="show the GitHub comment for a result, without posting")
    preview.add_argument("id", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

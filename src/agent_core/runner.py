from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agent_core.agents import dummy_code_agent, llm_code_agent
from agent_core.agents.code_agent_base import CodeAgentResult, IssueContext
from agent_core.git_ops import commit_if_needed, prepare_repo, push_branch
from agent_core.github_client import (
    comment_issue,
    create_or_update_pr,
    get_installation_token,
    get_issue,
    get_repo_info,
)
from agent_core.logging_setup import setup_logging
from agent_core.settings import get_settings
from agent_core.workspace import job_workspace

LOG = logging.getLogger(__name__)


def _apply_changes(
    repo_path: Path, issue: IssueContext, settings_apply_cmd: str | None
) -> CodeAgentResult:
    if settings_apply_cmd:
        LOG.info("Running apply command: %s", settings_apply_cmd)
        subprocess.run(settings_apply_cmd, cwd=str(repo_path), shell=True, check=True)
        title = f"[Agent] Fix issue #{issue.number}"
        body = f"Closes #{issue.number}\n\nAutomated changes by GitHub App."
        return CodeAgentResult(pr_title=title, pr_body=body)

    settings = get_settings()
    if settings.llm_service_url:
        return llm_code_agent.run_issue(issue, repo_path)

    return dummy_code_agent.run_issue(issue, repo_path)


def _maybe_comment(
    token: str | None,
    repo: str,
    issue_number: int,
    message: str,
) -> None:
    settings = get_settings()
    if not settings.comment_progress:
        return
    if token is None:
        return
    comment_issue(token, repo, issue_number, message)


def _handle_issue_opened_sync(
    repo: str, issue_number: int, installation_id: int, delivery_id: str | None
) -> dict[str, Any]:
    setup_logging()
    settings = get_settings()
    LOG.info("Handling issue %s in %s (delivery=%s)", issue_number, repo, delivery_id)

    token = get_installation_token(installation_id)
    _maybe_comment(token, repo, issue_number, "Started processing the issue.")

    repo_info = get_repo_info(token, repo)
    issue_payload = get_issue(token, repo, issue_number)
    issue = IssueContext(
        number=issue_number,
        title=issue_payload.get("title") or f"Issue #{issue_number}",
        body=issue_payload.get("body"),
    )

    branch = f"agent/issue-{issue_number}"
    job_id = f"issue-{issue_number}-{delivery_id or uuid.uuid4().hex[:8]}"

    with job_workspace(job_id=job_id) as base_dir:
        repo_path = prepare_repo(repo_info, token, base_dir=base_dir, branch=branch)

        _maybe_comment(token, repo, issue_number, "Applying changes.")
        result = _apply_changes(repo_path, issue, settings.apply_cmd)

        committed = commit_if_needed(
            repo_path, f"Agent: implement issue #{issue_number}"
        )
        if not committed:
            _maybe_comment(token, repo, issue_number, "No changes to commit.")
            return {"ok": True, "committed": False, "pr_url": None}

        push_branch(repo_path, branch)
        _maybe_comment(token, repo, issue_number, "Pushed branch and creating PR.")

        pr_url = create_or_update_pr(
            token, repo_info, branch, title=result.pr_title, body=result.pr_body
        )
        _maybe_comment(token, repo, issue_number, f"PR ready: {pr_url}")

    return {"ok": True, "committed": True, "pr_url": pr_url}


async def handle_issue_opened(
    repo: str, issue_number: int, installation_id: int, delivery_id: str | None
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _handle_issue_opened_sync, repo, issue_number, installation_id, delivery_id
    )


def handle_issue_opened_job(
    repo: str, issue_number: int, installation_id: int, delivery_id: str | None = None
) -> dict[str, Any]:
    return _handle_issue_opened_sync(repo, issue_number, installation_id, delivery_id)

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from agent_core.git_ops import commit_if_needed, prepare_repo, push_branch
from agent_core.github_client import (
    comment_issue,
    create_or_update_pr,
    get_installation_token,
    get_issue,
    get_repo_info,
)
from agent_core.logging_setup import setup_logging
from agent_core.orchestrator.run_issue_graph import CodeAgentResultV2, run_issue_graph
from agent_core.schemas import IssueContext
from agent_core.settings import get_settings
from agent_core.workspace import job_workspace

LOG = logging.getLogger(__name__)


def _apply_changes(repo_path: Path, issue: IssueContext) -> CodeAgentResultV2:
    settings = get_settings()

    def progress_cb(message: str) -> None:
        LOG.info("Agent progress: %s", message)

    return run_issue_graph(issue, repo_path, settings, progress_cb)


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
    repo: str,
    issue_number: int,
    installation_id: int,
    delivery_id: str | None,
    rerun_feedback: str | None = None,
) -> dict[str, Any]:
    setup_logging()
    LOG.info(
        "Handling issue %s in %s (delivery=%s, has_feedback=%s)",
        issue_number,
        repo,
        delivery_id,
        bool(rerun_feedback),
    )

    token = get_installation_token(installation_id)
    if rerun_feedback:
        _maybe_comment(
            token,
            repo,
            issue_number,
            "Started processing reviewer-requested rerun.",
        )
    else:
        _maybe_comment(token, repo, issue_number, "Started processing the issue.")

    repo_info = get_repo_info(token, repo)
    issue_payload = get_issue(token, repo, issue_number)
    issue_body = _compose_issue_body(issue_payload.get("body"), rerun_feedback)
    issue = IssueContext(
        number=issue_number,
        title=issue_payload.get("title") or f"Issue #{issue_number}",
        body=issue_body,
    )

    branch = f"agent/issue-{issue_number}"
    job_id = f"issue-{issue_number}-{delivery_id or uuid.uuid4().hex[:8]}"

    with job_workspace(job_id=job_id) as base_dir:
        repo_path = prepare_repo(repo_info, token, base_dir=base_dir, branch=branch)

        _maybe_comment(token, repo, issue_number, "Applying changes.")
        result = _apply_changes(repo_path, issue)

        if not result.checks_ok:
            _maybe_comment(
                token,
                repo,
                issue_number,
                "Checks failed. Skipping commit and PR creation.",
            )
            return {
                "ok": True,
                "committed": False,
                "pr_url": None,
                "checks_ok": False,
            }

        committed = commit_if_needed(
            repo_path, f"Agent: implement issue #{issue_number}"
        )
        if not committed:
            _maybe_comment(token, repo, issue_number, "No changes to commit.")
            return {"ok": True, "committed": False, "pr_url": None, "checks_ok": True}

        push_branch(repo_path, branch)
        _maybe_comment(token, repo, issue_number, "Pushed branch and creating PR.")

        pr_url = create_or_update_pr(
            token, repo_info, branch, title=result.pr_title, body=result.pr_body
        )
        _maybe_comment(token, repo, issue_number, f"PR ready: {pr_url}")

    return {"ok": True, "committed": True, "pr_url": pr_url, "checks_ok": True}


async def handle_issue_opened(
    repo: str,
    issue_number: int,
    installation_id: int,
    delivery_id: str | None,
    rerun_feedback: str | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _handle_issue_opened_sync,
        repo,
        issue_number,
        installation_id,
        delivery_id,
        rerun_feedback,
    )


def handle_issue_opened_job(
    repo: str,
    issue_number: int,
    installation_id: int,
    delivery_id: str | None = None,
    rerun_feedback: str | None = None,
) -> dict[str, Any]:
    return _handle_issue_opened_sync(
        repo,
        issue_number,
        installation_id,
        delivery_id,
        rerun_feedback,
    )


def _compose_issue_body(
    original_body: object, rerun_feedback: str | None, *, max_chars: int = 8000
) -> str | None:
    base = original_body if isinstance(original_body, str) else ""
    if not rerun_feedback:
        return base or None

    feedback = rerun_feedback.strip()
    if not feedback:
        return base or None
    if len(feedback) > max_chars:
        feedback = feedback[:max_chars].rstrip() + "\n...[feedback truncated]"

    suffix = (
        "\n\n---\n\n"
        "### Reviewer Feedback To Address\n"
        "This run was triggered automatically after reviewer requested changes. "
        "Address the feedback below with minimal, safe edits while keeping previous "
        "requirements satisfied.\n\n"
        f"{feedback}"
    )
    merged = (base + suffix).strip()
    return merged or None

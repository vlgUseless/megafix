from __future__ import annotations

import logging
from typing import Any

import requests
from github.PullRequest import PullRequest
from github.Repository import Repository
from github.WorkflowJob import WorkflowJob
from github.WorkflowRun import WorkflowRun

from megafix.shared.settings import get_settings

LOG = logging.getLogger(__name__)


def _auth_headers(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _collect_failed_job_logs(
    jobs: list[WorkflowJob], headers: dict[str, Any] | None
) -> dict[int, str | None]:
    failed_job_logs: dict[int, str | None] = {}
    max_bytes = get_settings().review_max_log_download_bytes
    for job in jobs:
        if job.conclusion not in ("failure", "timed_out"):
            continue
        LOG.debug("Getting logs for failed job: %s (id=%s)", job.name, job.id)
        try:
            logs_url = job.logs_url() if callable(job.logs_url) else job.logs_url
            failed_job_logs[job.id] = _download_log_excerpt(
                logs_url, headers=headers, max_bytes=max_bytes
            )
        except Exception as exc:
            LOG.warning("Failed to get logs for job %s: %s", job.name, exc)
            failed_job_logs[job.id] = None
    return failed_job_logs


def _download_log_excerpt(
    logs_url: str, *, headers: dict[str, Any] | None, max_bytes: int
) -> str:
    cap = max(1, max_bytes)
    downloaded = bytearray()
    truncated = False
    with requests.get(logs_url, headers=headers, timeout=30, stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = cap - len(downloaded)
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                downloaded.extend(chunk[:remaining])
                truncated = True
                break
            downloaded.extend(chunk)
    text = bytes(downloaded).decode("utf-8-sig", errors="replace")
    if truncated:
        text = f"{text}\n...[log truncated at {cap} bytes]"
    return text


def get_workflow_runs_and_logs(
    gh_repo: Repository,
    pull_request: PullRequest,
    token: str | None = None,
    *,
    head_sha: str | None = None,
) -> tuple[list[WorkflowRun], dict[int, str | None]]:
    """Return workflow runs for a PR and logs for failed jobs."""
    sha = head_sha or pull_request.head.sha
    LOG.debug("Getting workflow runs for SHA: %s", sha)

    runs = list(gh_repo.get_workflow_runs(head_sha=sha))
    LOG.debug("Found %s workflow runs", len(runs))

    headers = _auth_headers(token)
    failed_job_logs: dict[int, str | None] = {}
    for run in runs:
        if run.conclusion not in ("failure", "timed_out"):
            continue

        LOG.debug("Processing failed run: %s - %s", run.name, run.conclusion)
        failed_job_logs.update(_collect_failed_job_logs(list(run.jobs()), headers))

    LOG.debug("Found %s failed jobs with logs", len(failed_job_logs))
    return runs, failed_job_logs


def get_workflow_run_and_logs_by_id(
    gh_repo: Repository,
    run_id: int,
    token: str | None = None,
) -> tuple[WorkflowRun, dict[int, str | None]]:
    """Return a specific workflow run and failed job logs."""
    run = gh_repo.get_workflow_run(run_id)
    headers = _auth_headers(token)
    failed_job_logs = _collect_failed_job_logs(list(run.jobs()), headers)
    return run, failed_job_logs

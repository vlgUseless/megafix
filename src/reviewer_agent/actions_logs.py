from __future__ import annotations

import logging
from typing import Any

import requests
from github.PullRequest import PullRequest
from github.Repository import Repository
from github.WorkflowJob import WorkflowJob
from github.WorkflowRun import WorkflowRun

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
    for job in jobs:
        if job.conclusion not in ("failure", "timed_out"):
            continue
        LOG.debug("Getting logs for failed job: %s (id=%s)", job.name, job.id)
        try:
            logs_url = job.logs_url() if callable(job.logs_url) else job.logs_url
            r = requests.get(logs_url, headers=headers, timeout=30)
            r.raise_for_status()
            failed_job_logs[job.id] = r.content.decode("utf-8-sig")
        except Exception as exc:
            LOG.warning("Failed to get logs for job %s: %s", job.name, exc)
            failed_job_logs[job.id] = None
    return failed_job_logs


def get_workflow_runs_and_logs(
    gh_repo: Repository,
    pull_request: PullRequest,
    token: str | None = None,
) -> tuple[list[WorkflowRun], dict[int, str | None]]:
    """Return workflow runs for a PR and logs for failed jobs."""
    sha = pull_request.head.sha
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

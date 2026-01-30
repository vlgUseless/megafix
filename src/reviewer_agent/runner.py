from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from github import Github
from redis import Redis
from rq import Queue

from agent_core.github_client import (
    create_pull_request_review,
    get_installation_token,
    list_pull_requests_for_commit,
)
from agent_core.logging_setup import setup_logging
from agent_core.runner import handle_issue_opened_job
from agent_core.settings import get_settings
from reviewer_agent.actions_logs import (
    get_workflow_run_and_logs_by_id,
    get_workflow_runs_and_logs,
)
from reviewer_agent.review_agent import review_pull_request

LOG = logging.getLogger(__name__)
_MAX_RERUN_ATTEMPTS = 5


def _resolve_pr_number(
    token: str, repo_full_name: str, head_sha: str | None, base_branch: str | None
) -> int | None:
    if not head_sha:
        return None
    try:
        pulls = list_pull_requests_for_commit(token, repo_full_name, head_sha)
    except Exception as exc:
        LOG.warning("Failed to list PRs for SHA %s: %s", head_sha, exc)
        return None

    if not pulls:
        return None

    open_pulls = [pr for pr in pulls if pr.get("state") == "open"]
    candidates = open_pulls or pulls
    if base_branch:
        branch_filtered = [
            pr for pr in candidates if pr.get("base", {}).get("ref") == base_branch
        ]
        candidates = branch_filtered or candidates
    selected = candidates[0]
    pr_number = selected.get("number")
    return int(pr_number) if pr_number is not None else None


def handle_review_job(
    repo_full_name: str,
    installation_id: int,
    *,
    pr_number: int | None = None,
    head_sha: str | None = None,
    run_id: int | None = None,
    conclusion: str | None = None,
    base_branch: str | None = None,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    setup_logging()
    LOG.info(
        "Review job for %s (pr=%s, sha=%s, run_id=%s, conclusion=%s, delivery=%s)",
        repo_full_name,
        pr_number,
        head_sha,
        run_id,
        conclusion,
        delivery_id,
    )

    token = get_installation_token(installation_id)
    gh = Github(token)
    repository = gh.get_repo(repo_full_name)
    if pr_number is None:
        pr_number = _resolve_pr_number(token, repo_full_name, head_sha, base_branch)

    if pr_number is None:
        LOG.warning("No pull request found for review job; skipping.")
        return {"ok": False, "reason": "pr_not_found"}

    review_state_key = _review_state_key(head_sha, run_id)
    review_lock_key = _review_lock_key(repo_full_name, pr_number, review_state_key)
    lock_acquired = False
    if review_lock_key:
        lock_acquired = _acquire_review_lock(review_lock_key)
        if not lock_acquired:
            LOG.info(
                "Skipping duplicate review for %s#%s (sha=%s, run_id=%s)",
                repo_full_name,
                pr_number,
                head_sha,
                run_id,
            )
            return {
                "ok": True,
                "skipped": True,
                "pr_number": pr_number,
            }

    completed = False
    attempts = 0
    if review_state_key:
        completed, attempts = _register_review_attempt(
            repo_full_name, pr_number, review_state_key
        )
        if completed:
            LOG.info(
                "Review already completed for %s#%s (%s, attempts=%s); skipping.",
                repo_full_name,
                pr_number,
                review_state_key,
                attempts,
            )
            if lock_acquired and review_lock_key:
                _release_review_lock(review_lock_key)
            return {
                "ok": True,
                "skipped": True,
                "reason": "completed",
                "pr_number": pr_number,
                "attempts": attempts,
            }
    else:
        LOG.warning(
            "Missing review key for %s#%s (sha=%s, run_id=%s); state not tracked.",
            repo_full_name,
            pr_number,
            head_sha,
            run_id,
        )

    pull_request = repository.get_pull(pr_number)

    issue_number = _extract_issue_number(pull_request.body)
    if issue_number and issue_number != pull_request.number:
        try:
            issue = repository.get_issue(issue_number)
        except Exception as exc:
            LOG.warning("Failed to fetch issue #%s: %s", issue_number, exc)
            issue = repository.get_issue(pull_request.number)
    else:
        issue = repository.get_issue(pull_request.number)

    if run_id is not None:
        try:
            run, failed_job_logs = get_workflow_run_and_logs_by_id(
                repository, run_id, token=token
            )
            workflow_runs = [run]
        except Exception as exc:
            LOG.warning("Failed to fetch workflow run %s: %s", run_id, exc)
            workflow_runs, failed_job_logs = get_workflow_runs_and_logs(
                repository, pull_request, token=token
            )
    else:
        workflow_runs, failed_job_logs = get_workflow_runs_and_logs(
            repository, pull_request, token=token
        )
    review_comment, approve, verdict = review_pull_request(
        pull_request, issue, workflow_runs, failed_job_logs
    )

    event = _select_review_event(conclusion, approve, failed_job_logs)
    try:
        LOG.info(
            "Publishing review: event=%s body_len=%d", event, len(review_comment or "")
        )
        create_pull_request_review(
            token,
            repo_full_name,
            pull_request.number,
            body=review_comment,
            event=event,
        )
    except Exception:
        if lock_acquired and review_lock_key:
            _release_review_lock(review_lock_key)
        LOG.exception(
            "Failed to publish review for %s#%s (sha=%s, run_id=%s)",
            repo_full_name,
            pull_request.number,
            head_sha,
            run_id,
        )
        raise

    if review_state_key:
        _mark_review_completed(repo_full_name, pull_request.number, review_state_key)

    rerun_triggered = False
    rerun_attempts = None
    if verdict == "request_changes":
        rerun_triggered, rerun_attempts = _maybe_rerun_code_agent(
            repo_full_name=repo_full_name,
            issue_number=issue.number,
            installation_id=installation_id,
            review_key=review_state_key,
        )
    else:
        _mark_rerun_completed(repo_full_name, issue.number)

    return {
        "ok": True,
        "pr_number": pull_request.number,
        "approve": approve,
        "review_comment": review_comment,
        "verdict": verdict,
        "rerun_triggered": rerun_triggered,
        "rerun_attempts": rerun_attempts,
    }


def _extract_issue_number(body: str | None) -> int | None:
    if not body:
        return None
    match = re.search(
        r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)",
        body,
    )
    if not match:
        return None
    return int(match.group(1))


def _select_review_event(
    conclusion: str | None, approve: bool, failed_job_logs: dict[int, str | None]
) -> str:
    return "COMMENT"


def _review_state_key(head_sha: str | None, run_id: int | None) -> str | None:
    if run_id is not None:
        return f"run:{run_id}"
    if head_sha:
        return f"sha:{head_sha}"
    return None


def _review_lock_key(
    repo_full_name: str,
    pr_number: int,
    review_state_key: str | None,
) -> str | None:
    if not review_state_key:
        return None
    return f"reviewed:{repo_full_name}:{pr_number}:{review_state_key}"


def _acquire_review_lock(key: str) -> bool:
    settings = get_settings()
    redis_conn = Redis.from_url(settings.redis_url)
    return bool(redis_conn.set(key, "1", nx=True, ex=settings.rq_result_ttl))


def _release_review_lock(key: str) -> None:
    settings = get_settings()
    redis_conn = Redis.from_url(settings.redis_url)
    try:
        redis_conn.delete(key)
    except Exception as exc:
        LOG.warning("Failed to release review lock %s: %s", key, exc)


def _review_state_db_path() -> Path:
    settings = get_settings()
    path = settings.review_state_db
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_review_state_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_state (
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            review_key TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repo, pr_number, review_key)
        )
        """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(review_state)")}
    if "review_key" not in columns:
        conn.execute("ALTER TABLE review_state RENAME TO review_state_v1")
        conn.execute("""
            CREATE TABLE review_state (
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                review_key TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (repo, pr_number, review_key)
            )
            """)
        conn.execute("""
            INSERT INTO review_state (repo, pr_number, review_key, attempts, completed)
            SELECT repo, pr_number, 'legacy', attempts, completed
            FROM review_state_v1
            """)
        conn.execute("DROP TABLE review_state_v1")


def _ensure_rerun_state_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rerun_state (
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repo, issue_number)
        )
        """)


def _register_review_attempt(
    repo_full_name: str, pr_number: int, review_key: str
) -> tuple[bool, int]:
    db_path = _review_state_db_path()
    with sqlite3.connect(db_path, timeout=5) as conn:
        _ensure_review_state_table(conn)
        conn.execute(
            """
            INSERT INTO review_state (repo, pr_number, review_key, attempts, completed)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(repo, pr_number, review_key) DO UPDATE SET
                attempts = CASE
                    WHEN completed = 1 THEN attempts
                    ELSE attempts + 1
                END
            """,
            (repo_full_name, pr_number, review_key),
        )
        row = conn.execute(
            """
            SELECT completed, attempts
            FROM review_state
            WHERE repo = ? AND pr_number = ? AND review_key = ?
            """,
            (repo_full_name, pr_number, review_key),
        ).fetchone()
    completed = bool(row[0]) if row else False
    attempts = int(row[1]) if row else 0
    return completed, attempts


def _mark_review_completed(
    repo_full_name: str, pr_number: int, review_key: str
) -> None:
    db_path = _review_state_db_path()
    with sqlite3.connect(db_path, timeout=5) as conn:
        _ensure_review_state_table(conn)
        conn.execute(
            """
            INSERT INTO review_state (repo, pr_number, review_key, attempts, completed)
            VALUES (?, ?, ?, 1, 1)
            ON CONFLICT(repo, pr_number, review_key) DO UPDATE SET completed = 1
            """,
            (repo_full_name, pr_number, review_key),
        )


def _register_rerun_attempt(
    repo_full_name: str, issue_number: int, max_attempts: int
) -> tuple[bool, int]:
    db_path = _review_state_db_path()
    with sqlite3.connect(db_path, timeout=5) as conn:
        _ensure_rerun_state_table(conn)
        row = conn.execute(
            """
            SELECT attempts, completed
            FROM rerun_state
            WHERE repo = ? AND issue_number = ?
            """,
            (repo_full_name, issue_number),
        ).fetchone()
        if row:
            attempts = int(row[0])
            completed = bool(row[1])
        else:
            attempts = 0
            completed = False

        if completed or attempts >= max_attempts:
            return False, attempts

        attempts += 1
        conn.execute(
            """
            INSERT INTO rerun_state (repo, issue_number, attempts, completed)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(repo, issue_number) DO UPDATE SET attempts = ?
            """,
            (repo_full_name, issue_number, attempts, attempts),
        )
        return True, attempts


def _mark_rerun_completed(repo_full_name: str, issue_number: int) -> None:
    db_path = _review_state_db_path()
    with sqlite3.connect(db_path, timeout=5) as conn:
        _ensure_rerun_state_table(conn)
        conn.execute(
            """
            INSERT INTO rerun_state (repo, issue_number, attempts, completed)
            VALUES (?, ?, 0, 1)
            ON CONFLICT(repo, issue_number) DO UPDATE SET completed = 1
            """,
            (repo_full_name, issue_number),
        )


def _maybe_rerun_code_agent(
    *,
    repo_full_name: str,
    issue_number: int,
    installation_id: int,
    review_key: str | None,
) -> tuple[bool, int | None]:
    should_rerun, attempts = _register_rerun_attempt(
        repo_full_name, issue_number, _MAX_RERUN_ATTEMPTS
    )
    if not should_rerun:
        LOG.info(
            "Rerun skipped for %s#%s (attempts=%s, max=%s)",
            repo_full_name,
            issue_number,
            attempts,
            _MAX_RERUN_ATTEMPTS,
        )
        return False, attempts

    settings = get_settings()
    redis_conn = Redis.from_url(settings.redis_url)
    q = Queue(settings.rq_queue, connection=redis_conn)
    delivery_id = f"review-rerun:{review_key or 'manual'}:{attempts}"
    q.enqueue(
        handle_issue_opened_job,
        repo_full_name,
        issue_number,
        installation_id,
        delivery_id,
        job_timeout=settings.rq_job_timeout,
        result_ttl=settings.rq_result_ttl,
        failure_ttl=settings.rq_failure_ttl,
    )
    LOG.info(
        "Queued rerun for %s#%s (attempt=%s/%s)",
        repo_full_name,
        issue_number,
        attempts,
        _MAX_RERUN_ATTEMPTS,
    )
    return True, attempts

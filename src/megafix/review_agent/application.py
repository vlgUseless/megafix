import logging
from typing import Any, TypedDict

from github.Issue import Issue
from github.PullRequest import PullRequest
from github.WorkflowRun import WorkflowRun

from megafix.infra.llm_clients import (
    LLMServiceError,
    ReviewFinding,
    StructuredReview,
    summarize_review,
)
from megafix.shared.settings import get_settings

LOG = logging.getLogger(__name__)


class FailedLogSummary(TypedDict):
    job_id: int
    log_excerpt: str | None
    truncated: bool


def review_pull_request(
    pull_request: PullRequest,
    issue: Issue,
    workflow_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> tuple[str, bool, str | None, bool]:
    """Generate an LLM-assisted review summary and normalized verdict."""
    LOG.debug("Reviewing PR #%s: %s", pull_request.number, pull_request.title)
    LOG.debug("Related issue #%s: %s", issue.number, issue.title)

    comments = list(pull_request.get_issue_comments())
    LOG.debug("Found %s comments on PR", len(comments))
    LOG.debug("Found %s workflow runs", len(workflow_runs))
    LOG.debug("Found %s failed jobs with logs", len(failed_job_logs))

    failed_runs = [
        run for run in workflow_runs if run.conclusion in ("failure", "timed_out")
    ]

    get_files = getattr(pull_request, "get_files", None)
    files = list(get_files()) if callable(get_files) else []
    total_additions = sum(f.additions for f in files)
    total_deletions = sum(f.deletions for f in files)
    total_changes = sum(f.changes for f in files)
    file_names = [f.filename for f in files[:10]]

    ci_summary = _build_ci_summary(workflow_runs, failed_job_logs)
    diff = _build_pr_diff(files)

    structured_summary: StructuredReview | None = None
    try:
        structured_summary = summarize_review(diff, ci_summary, issue)
    except LLMServiceError as exc:
        LOG.warning("LLM review failed, falling back to deterministic summary: %s", exc)
    except Exception:
        LOG.exception("Unexpected error while calling LLM review.")

    has_blocking_findings = bool(
        structured_summary and structured_summary.blocking_findings
    )
    verdict = structured_summary.verdict if structured_summary else None

    review_comment = _format_review_comment(
        pull_request=pull_request,
        issue=issue,
        comments_count=len(comments),
        workflow_runs=workflow_runs,
        failed_runs=failed_runs,
        failed_job_logs=failed_job_logs,
        files_count=len(files),
        total_additions=total_additions,
        total_deletions=total_deletions,
        total_changes=total_changes,
        file_names=file_names,
        summary=structured_summary,
        has_blocking_findings=has_blocking_findings,
    )

    ci_pass = bool(workflow_runs) and not failed_runs and not failed_job_logs
    approve = ci_pass and not has_blocking_findings
    return review_comment, approve, verdict, has_blocking_findings


def run_review_agent(
    pull_request: PullRequest,
    issue: Issue,
    workflow_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> tuple[str, bool, str | None, bool]:
    """Backward-compatible wrapper for review_pull_request."""
    return review_pull_request(pull_request, issue, workflow_runs, failed_job_logs)


def _build_pr_diff(files: list[Any]) -> str:
    settings = get_settings()
    max_diff_chars = settings.review_max_diff_chars
    max_patch_chars = settings.review_max_patch_chars
    chunks: list[str] = []
    total = 0
    for file in files:
        filename = getattr(file, "filename", "unknown")
        status = getattr(file, "status", "unknown")
        additions = getattr(file, "additions", 0)
        deletions = getattr(file, "deletions", 0)
        header = (
            f"diff --git a/{filename} b/{filename}\n"
            f"--- a/{filename}\n"
            f"+++ b/{filename}\n"
            f"# status: {status}, additions: {additions}, deletions: {deletions}\n"
        )
        patch = getattr(file, "patch", None) or ""
        if not patch:
            patch = "# patch unavailable (binary or too large)\n"
        if len(patch) > max_patch_chars:
            patch = patch[:max_patch_chars] + "\n# ...patch truncated\n"
        chunk = f"{header}{patch}\n"
        if total + len(chunk) > max_diff_chars:
            chunks.append("# ...diff truncated due to size\n")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "".join(chunks).strip()


def _build_ci_summary(
    workflow_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> dict[str, Any]:
    settings = get_settings()
    max_log_chars = settings.review_max_log_chars
    runs_summary = []
    for run in workflow_runs:
        runs_summary.append(
            {
                "id": getattr(run, "id", None),
                "name": getattr(run, "name", None),
                "status": getattr(run, "status", None),
                "conclusion": getattr(run, "conclusion", None),
                "html_url": getattr(run, "html_url", None),
            }
        )

    failed_logs_summary: list[FailedLogSummary] = []
    for job_id, log in failed_job_logs.items():
        if log is None:
            failed_logs_summary.append(
                {"job_id": job_id, "log_excerpt": None, "truncated": False}
            )
            continue
        excerpt = log[:max_log_chars]
        failed_logs_summary.append(
            {
                "job_id": job_id,
                "log_excerpt": excerpt,
                "truncated": len(log) > max_log_chars,
            }
        )

    return {
        "runs": runs_summary,
        "failed_job_logs": failed_logs_summary,
    }


def _format_review_comment(
    *,
    pull_request: PullRequest,
    issue: Issue,
    comments_count: int,
    workflow_runs: list[WorkflowRun],
    failed_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
    files_count: int,
    total_additions: int,
    total_deletions: int,
    total_changes: int,
    file_names: list[str],
    summary: StructuredReview | None,
    has_blocking_findings: bool,
) -> str:
    _ = comments_count
    ci_ok = bool(workflow_runs) and not failed_runs and not failed_job_logs
    if summary and has_blocking_findings:
        verdict_text = "❌ Request changes"
    elif summary:
        verdict_text = "✅ Approve"
    elif ci_ok:
        verdict_text = "⚪ Manual review needed (LLM unavailable, CI passing)"
    else:
        verdict_text = "⚠️ Manual review needed"

    if ci_ok:
        ci_text = "✅ passing"
    elif not workflow_runs:
        ci_text = "⚪ no workflow runs"
    else:
        ci_text = "❌ failing"

    comment = "## Megafix Review\n\n"
    comment += f"**Verdict:** {verdict_text}\n"
    comment += (
        f"**CI:** {ci_text} "
        f"({len(workflow_runs)} runs, {len(failed_runs)} failed, "
        f"{len(failed_job_logs)} failed job logs)\n"
    )
    comment += (
        f"**Diff scope:** {files_count} files "
        f"(+{total_additions}/-{total_deletions}, {total_changes} total)\n"
    )

    comment += "\n<details>\n"
    comment += "<summary>Context</summary>\n\n"
    comment += f"- PR: {pull_request.title}\n"
    if issue.number != pull_request.number:
        comment += f"- Issue: #{issue.number} — {issue.title}\n"
    else:
        comment += f"- Issue: #{issue.number}\n"
    if file_names:
        shown = min(len(file_names), 8)
        rendered = ", ".join(f"`{name}`" for name in file_names[:shown])
        comment += f"- Touched files (showing {shown} of {files_count}): {rendered}\n"
        remaining = files_count - shown
        if remaining > 0:
            comment += f"- Plus {remaining} more file(s).\n"
    comment += "\n</details>\n\n"

    comment += "### Summary\n"
    if summary and summary.summary:
        comment += _render_text_bullets(summary.summary)
    elif summary:
        comment += "- No summary provided.\n"
    else:
        comment += "- LLM review unavailable. Check diff manually.\n"

    comment += "\n### Blocking findings\n"
    if summary:
        comment += _render_findings(summary.blocking_findings)
    else:
        comment += "- Unknown (LLM unavailable).\n"

    comment += "\n### Non-blocking findings\n"
    if summary:
        comment += _render_findings(summary.non_blocking_findings)
    else:
        comment += "- Unknown (LLM unavailable).\n"

    comment += "\n### Tests\n"
    if summary and summary.tests:
        comment += _render_text_bullets(summary.tests)
    else:
        comment += _render_default_tests(workflow_runs, failed_runs, failed_job_logs)

    return comment


def _render_findings(findings: tuple[ReviewFinding, ...]) -> str:
    if not findings:
        return "- None.\n"
    lines: list[str] = []
    for finding in findings:
        severity = finding.severity.upper()
        location = ""
        if finding.file and finding.line:
            location = f" ({finding.file}:{finding.line})"
        elif finding.file:
            location = f" ({finding.file})"
        lines.append(f"- [{severity}] {finding.title}{location}: {finding.details}")
    return "\n".join(lines) + "\n"


def _render_text_bullets(items: tuple[str, ...]) -> str:
    if not items:
        return "- None.\n"
    return "\n".join(f"- {item}" for item in items) + "\n"


def _render_default_tests(
    workflow_runs: list[WorkflowRun],
    failed_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> str:
    if not workflow_runs:
        return "- No workflow runs were available.\n"
    if failed_runs or failed_job_logs:
        lines = [f"- CI reports failures ({len(failed_runs)} failed run(s))."]
        if failed_job_logs:
            lines.append(
                f"- Collected {len(failed_job_logs)} failed job log excerpt(s)."
            )
        return "\n".join(lines) + "\n"
    return "- CI passed for the current head SHA.\n"

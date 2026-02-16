import logging
import re
from typing import Any, TypedDict

from github.Issue import Issue
from github.PullRequest import PullRequest
from github.WorkflowRun import WorkflowRun

from megafix.infra.llm_clients import LLMServiceError, summarize_review
from megafix.shared.settings import get_settings

LOG = logging.getLogger(__name__)
_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:\*\*)?(verdict|вердикт)(?:\*\*)?\s*:?\s*(.*)\s*$",
    re.IGNORECASE,
)


class FailedLogSummary(TypedDict):
    job_id: int
    log_excerpt: str | None
    truncated: bool


def review_pull_request(
    pull_request: PullRequest,
    issue: Issue,
    workflow_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> tuple[str, bool, str | None]:
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

    summary = None
    try:
        summary = summarize_review(diff, ci_summary, issue)
    except LLMServiceError as exc:
        LOG.warning("LLM review failed, falling back to deterministic summary: %s", exc)
    except Exception:
        LOG.exception("Unexpected error while calling LLM review.")

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
        summary=summary,
    )

    ci_pass = bool(workflow_runs) and not failed_runs and not failed_job_logs
    # LLM verdict is advisory; CI status remains a hard gate for approve.
    verdict = _extract_verdict(summary) if summary else None
    if verdict == "request_changes":
        approve = False
    elif verdict == "approve":
        approve = ci_pass
    else:
        approve = ci_pass

    return review_comment, approve, verdict


def run_review_agent(
    pull_request: PullRequest,
    issue: Issue,
    workflow_runs: list[WorkflowRun],
    failed_job_logs: dict[int, str | None],
) -> tuple[str, bool, str | None]:
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
                "id": run.id,
                "name": run.name,
                "status": run.status,
                "conclusion": run.conclusion,
                "html_url": run.html_url,
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
    summary: str | None,
) -> str:
    verdict = _extract_verdict(summary)
    ci_ok = bool(workflow_runs) and not failed_runs and not failed_job_logs
    if verdict == "approve":
        verdict_text = "✅ Approve"
    elif verdict == "request_changes":
        verdict_text = "❌ Request changes"
    elif ci_ok:
        verdict_text = "✅ CI passed, no blocking findings"
    else:
        verdict_text = "⚠️ Needs attention"

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

    comment += "### Assessment\n"

    if summary:
        cleaned_summary = _strip_verdict_block(summary).strip()
        if cleaned_summary:
            comment += cleaned_summary + "\n\n"
    else:
        comment += (
            "- LLM review unavailable. Please check CI and PR changes manually.\n"
        )
        comment += f"- Standard verdict: {verdict_text}.\n"
        if failed_runs:
            comment += f"- {len(failed_runs)} workflow run(s) reported failures.\n"
        if failed_job_logs:
            comment += f"- {len(failed_job_logs)} failed job log(s) were collected.\n"
    return comment


def _extract_verdict(summary: str | None) -> str | None:
    if not summary:
        return None
    lowered = summary.lower()
    if "verdict" in lowered or "вердикт" in lowered:
        lines = lowered.splitlines()
        for line in lines:
            if "verdict" in line or "вердикт" in line:
                if "request changes" in line or "changes requested" in line:
                    return "request_changes"
                if "approve" in line or "lgtm" in line:
                    return "approve"
    if "request changes" in lowered or "changes requested" in lowered:
        return "request_changes"
    if "approve" in lowered or "lgtm" in lowered:
        return "approve"
    return None


def _strip_verdict_block(summary: str) -> str:
    lines = summary.splitlines()
    kept: list[str] = []
    skip_next_value = False
    for line in lines:
        stripped = line.strip()
        match = _VERDICT_LINE_RE.match(stripped)
        if match:
            inline_tail = match.group(2).strip()
            skip_next_value = bool(not inline_tail)
            continue
        if skip_next_value:
            if not stripped:
                continue
            if _looks_like_verdict_value(stripped):
                skip_next_value = False
                continue
            skip_next_value = False
        kept.append(line)
    return _squash_blank_lines("\n".join(kept).strip())


def _looks_like_verdict_value(text: str) -> bool:
    normalized = text.strip().strip("*_`")
    normalized = normalized.lstrip("-* ").strip().strip("*_`").lower()
    return normalized in {
        "approve",
        "approved",
        "lgtm",
        "request changes",
        "changes requested",
        "request_changes",
    }


def _squash_blank_lines(text: str) -> str:
    if not text:
        return ""
    parts = text.splitlines()
    out: list[str] = []
    blank = False
    for line in parts:
        if line.strip():
            out.append(line)
            blank = False
            continue
        if not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()

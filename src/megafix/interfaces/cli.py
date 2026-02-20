from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from github import Auth, Github

from megafix.code_agent.patches_engine import apply_patches as apply_unified_patches
from megafix.infra.github_client import (
    create_pull_request_review,
    get_installation_id,
    get_installation_token,
)
from megafix.interfaces.cli_parsing import parse_issue_url, parse_pr_url
from megafix.interfaces.workers import handle_issue_opened_job
from megafix.review_agent.actions_logs import get_workflow_runs_and_logs
from megafix.review_agent.application import review_pull_request
from megafix.shared.logging_setup import setup_logging
from megafix.shared.settings import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True)

_PATCH_FILE_OPTION = typer.Option(..., "--patch-file", "-p")
_REPO_PATH_OPTION = typer.Option(Path("."), "--repo-path")


def _build_github_client(token: str) -> Github:
    """Use modern PyGithub auth API with backward-compatible fallback."""
    try:
        return Github(auth=Auth.Token(token))
    except TypeError:
        # Compatibility for tests/mocks or older PyGithub signatures.
        return Github(token)


def _resolve_review_issue(repository: Any, pull_request: Any) -> Any:
    body = getattr(pull_request, "body", "") or ""
    match = re.search(
        r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)",
        body,
    )
    if match:
        issue_number = int(match.group(1))
        if issue_number != pull_request.number:
            try:
                return repository.get_issue(issue_number)
            except Exception:
                pass
    return repository.get_issue(pull_request.number)


@app.command("run-issue")
def run_issue(issue_url: str = typer.Option(..., "--issue-url")) -> None:
    """Run the issue pipeline synchronously."""
    get_settings()
    setup_logging()
    owner, repo, issue_number = parse_issue_url(issue_url)
    installation_id = get_installation_id(owner, repo)
    full_repo = f"{owner}/{repo}"
    result = handle_issue_opened_job(full_repo, issue_number, installation_id)
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("review-pr")
def review_pr(
    pr_url: str = typer.Option(..., "--pr-url"),
    publish: bool = typer.Option(False, "--publish"),
) -> None:
    """Run the review agent and print output; optionally publish review comment."""
    get_settings()
    setup_logging()
    owner, repo, pr_number = parse_pr_url(pr_url)
    installation_id = get_installation_id(owner, repo)
    token = get_installation_token(installation_id)
    repo_full_name = f"{owner}/{repo}"

    gh = _build_github_client(token)
    repository = gh.get_repo(repo_full_name)
    pull_request = repository.get_pull(pr_number)
    issue = _resolve_review_issue(repository, pull_request)

    workflow_runs, failed_job_logs = get_workflow_runs_and_logs(
        repository, pull_request, token=token
    )
    review_comment, approve, verdict, has_blocking_findings = review_pull_request(
        pull_request, issue, workflow_runs, failed_job_logs
    )
    if publish:
        create_pull_request_review(
            token,
            repo_full_name,
            pr_number,
            body=review_comment,
            event="COMMENT",
        )
        typer.echo("Published review comment to PR.")

    typer.echo(review_comment)
    typer.echo(f"\nApprove: {approve}")
    typer.echo(f"Verdict: {verdict or 'n/a'}")
    typer.echo(f"Blocking findings: {has_blocking_findings}")


@app.command("apply-patches")
def apply_patches_cmd(
    patch_file: list[Path] = _PATCH_FILE_OPTION,
    repo_path: Path = _REPO_PATH_OPTION,
) -> None:
    """Apply unified diff patches with validation/policy checks."""
    patches = [path.read_text(encoding="utf-8") for path in patch_file]
    result = apply_unified_patches(patches, repo_path=repo_path)
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.ok:
        raise typer.Exit(code=2)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

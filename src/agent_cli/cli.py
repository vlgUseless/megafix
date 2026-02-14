from __future__ import annotations

import json
from pathlib import Path

import typer
from github import Github

from agent_cli.parsing import parse_issue_url, parse_pr_url
from agent_core.github_client import get_installation_id, get_installation_token
from agent_core.logging_setup import setup_logging
from agent_core.patch_engine import apply_patches as apply_unified_patches
from agent_core.runner import handle_issue_opened_job
from agent_core.settings import get_settings
from reviewer_agent.actions_logs import get_workflow_runs_and_logs
from reviewer_agent.review_agent import review_pull_request

app = typer.Typer(add_completion=False, no_args_is_help=True)

_PATCH_FILE_OPTION = typer.Option(..., "--patch-file", "-p")
_REPO_PATH_OPTION = typer.Option(Path("."), "--repo-path")


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
def review_pr(pr_url: str = typer.Option(..., "--pr-url")) -> None:
    """Run the review agent and print its output."""
    get_settings()
    setup_logging()
    owner, repo, pr_number = parse_pr_url(pr_url)
    installation_id = get_installation_id(owner, repo)
    token = get_installation_token(installation_id)

    gh = Github(token)
    repository = gh.get_repo(f"{owner}/{repo}")
    pull_request = repository.get_pull(pr_number)
    issue = repository.get_issue(pr_number)

    workflow_runs, failed_job_logs = get_workflow_runs_and_logs(
        repository, pull_request, token=token
    )
    review_comment, approve, verdict = review_pull_request(
        pull_request, issue, workflow_runs, failed_job_logs
    )

    typer.echo(review_comment)
    typer.echo(f"\nApprove: {approve}")
    typer.echo(f"Verdict: {verdict or 'n/a'}")


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

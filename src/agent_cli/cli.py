from __future__ import annotations

import json

import typer
from github import Github

from agent_cli.parsing import parse_issue_url, parse_pr_url
from agent_core.github_client import get_installation_id, get_installation_token
from agent_core.logging_setup import setup_logging
from agent_core.runner import handle_issue_opened_job
from agent_core.settings import get_settings
from reviewer_agent.actions_logs import get_workflow_runs_and_logs
from reviewer_agent.review_agent import review_pull_request

app = typer.Typer(add_completion=False, no_args_is_help=True)


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
    # NOTE: This is the PR "issue" object; later we should resolve the source Issue
    # from PR body (e.g., "Closes #N") or linked issues.
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()

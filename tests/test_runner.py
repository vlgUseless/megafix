from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from agent_core import runner
from agent_core.github_client import RepoInfo
from agent_core.orchestrator.run_issue_graph import CodeAgentResultV2


def _setup_common_runner_mocks(monkeypatch, tmp_path: Path, *, checks_ok: bool) -> None:
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(apply_cmd=None, comment_progress=False),
    )
    monkeypatch.setattr(runner, "get_installation_token", lambda installation_id: "tok")
    monkeypatch.setattr(
        runner,
        "get_repo_info",
        lambda token, repo: RepoInfo(
            full_name=repo,
            default_branch="main",
            owner="owner",
            name="repo",
        ),
    )
    monkeypatch.setattr(
        runner,
        "get_issue",
        lambda token, repo, issue_number: {"title": "Title", "body": "Body"},
    )

    @contextmanager
    def fake_workspace(job_id: str | None = None):
        _ = job_id
        yield tmp_path

    monkeypatch.setattr(runner, "job_workspace", fake_workspace)
    monkeypatch.setattr(
        runner,
        "prepare_repo",
        lambda repo_info, token, base_dir, branch: tmp_path,
    )
    monkeypatch.setattr(
        runner,
        "_apply_changes",
        lambda repo_path, issue, _settings_apply_cmd: CodeAgentResultV2(
            pr_title="PR title",
            pr_body="PR body",
            final_message="done",
            checks_ok=checks_ok,
            iterations=1,
        ),
    )


def test_handle_issue_opened_skips_commit_and_pr_when_checks_fail(
    monkeypatch, tmp_path: Path
) -> None:
    _setup_common_runner_mocks(monkeypatch, tmp_path, checks_ok=False)
    calls = {"commit": 0, "push": 0, "pr": 0}

    def fake_commit_if_needed(repo_path: Path, message: str) -> bool:
        _ = (repo_path, message)
        calls["commit"] += 1
        return True

    def fake_push_branch(repo_path: Path, branch: str) -> None:
        _ = (repo_path, branch)
        calls["push"] += 1

    def fake_create_or_update_pr(*args, **kwargs) -> str:
        _ = (args, kwargs)
        calls["pr"] += 1
        return "https://example.com/pr/1"

    monkeypatch.setattr(runner, "commit_if_needed", fake_commit_if_needed)
    monkeypatch.setattr(runner, "push_branch", fake_push_branch)
    monkeypatch.setattr(runner, "create_or_update_pr", fake_create_or_update_pr)

    result = runner._handle_issue_opened_sync("owner/repo", 55, 1, "delivery")

    assert result["ok"] is True
    assert result["checks_ok"] is False
    assert result["committed"] is False
    assert result["pr_url"] is None
    assert calls == {"commit": 0, "push": 0, "pr": 0}


def test_handle_issue_opened_creates_pr_when_checks_pass(
    monkeypatch, tmp_path: Path
) -> None:
    _setup_common_runner_mocks(monkeypatch, tmp_path, checks_ok=True)
    calls = {"commit": 0, "push": 0, "pr": 0}

    def fake_commit_if_needed(repo_path: Path, message: str) -> bool:
        _ = (repo_path, message)
        calls["commit"] += 1
        return True

    def fake_push_branch(repo_path: Path, branch: str) -> None:
        _ = (repo_path, branch)
        calls["push"] += 1

    def fake_create_or_update_pr(*args, **kwargs) -> str:
        _ = (args, kwargs)
        calls["pr"] += 1
        return "https://example.com/pr/1"

    monkeypatch.setattr(runner, "commit_if_needed", fake_commit_if_needed)
    monkeypatch.setattr(runner, "push_branch", fake_push_branch)
    monkeypatch.setattr(runner, "create_or_update_pr", fake_create_or_update_pr)

    result = runner._handle_issue_opened_sync("owner/repo", 55, 1, "delivery")

    assert result["ok"] is True
    assert result["checks_ok"] is True
    assert result["committed"] is True
    assert result["pr_url"] == "https://example.com/pr/1"
    assert calls == {"commit": 1, "push": 1, "pr": 1}

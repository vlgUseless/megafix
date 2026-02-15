from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reviewer_agent import runner


def test_maybe_rerun_code_agent_enqueues_feedback(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_register_rerun_attempt", lambda *args: (True, 1))
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(
            review_rerun_max_attempts=3,
            redis_url="redis://test",
            rq_queue="default",
            rq_job_timeout="20m",
            rq_result_ttl=3600,
            rq_failure_ttl=86400,
        ),
    )

    class _FakeRedis:
        pass

    monkeypatch.setattr(
        runner,
        "Redis",
        SimpleNamespace(from_url=lambda _url: _FakeRedis()),
    )
    captured: dict[str, object] = {}

    class _FakeQueue:
        def __init__(self, _name, connection):
            captured["connection"] = connection

        def enqueue(self, fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(runner, "Queue", _FakeQueue)

    triggered, attempts = runner._maybe_rerun_code_agent(
        repo_full_name="owner/repo",
        issue_number=57,
        installation_id=123,
        review_key="sha:abc",
        review_feedback="Please address edge case and add tests.",
    )

    assert triggered is True
    assert attempts == 1
    assert captured["fn"] is runner.handle_issue_opened_job
    args = captured["args"]
    assert isinstance(args, tuple)
    assert args[0:4] == ("owner/repo", 57, 123, "review-rerun:sha:abc:1")
    assert args[4] == "Please address edge case and add tests."


def test_maybe_rerun_code_agent_skips_without_review_key(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(
            review_rerun_max_attempts=3,
            redis_url="redis://test",
            rq_queue="default",
            rq_job_timeout="20m",
            rq_result_ttl=3600,
            rq_failure_ttl=86400,
        ),
    )
    called = {"queue": False}

    class _FakeQueue:
        def __init__(self, _name, connection):
            called["queue"] = True

        def enqueue(self, fn, *args, **kwargs):
            called["queue"] = True

    monkeypatch.setattr(runner, "Queue", _FakeQueue)

    triggered, attempts = runner._maybe_rerun_code_agent(
        repo_full_name="owner/repo",
        issue_number=57,
        installation_id=123,
        review_key=None,
        review_feedback="feedback",
    )

    assert triggered is False
    assert attempts is None
    assert called["queue"] is False


def test_review_state_key_prefers_sha() -> None:
    assert runner._review_state_key("abc", 123) == "sha:abc"
    assert runner._review_state_key(None, 123) == "run:123"
    assert runner._review_state_key(None, None) is None


def test_is_agent_issue_pr_requires_issue_and_branch_prefix() -> None:
    pr = SimpleNamespace(head=SimpleNamespace(ref="agent/issue-57"))
    assert runner._is_agent_issue_pr(pr, 57) is True
    assert runner._is_agent_issue_pr(pr, None) is False
    non_agent_pr = SimpleNamespace(head=SimpleNamespace(ref="feature/my-branch"))
    assert runner._is_agent_issue_pr(non_agent_pr, 57) is False


def test_has_pending_runs_detects_non_completed_status() -> None:
    completed = [
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="completed"),
    ]
    pending = [SimpleNamespace(status="queued"), SimpleNamespace(status="completed")]
    missing = [SimpleNamespace(status=None)]
    assert runner._has_pending_runs(completed) is False
    assert runner._has_pending_runs(pending) is True
    assert runner._has_pending_runs(missing) is True


def test_register_rerun_attempt_scoped_by_review_key(monkeypatch) -> None:
    monkey_settings = SimpleNamespace(review_state_db=Path(":memory:"))
    original_get_settings = runner.get_settings
    runner.get_settings = lambda: monkey_settings
    shared_conn = runner.sqlite3.connect(":memory:")
    monkeypatch.setattr(
        runner.sqlite3,
        "connect",
        lambda *args, **kwargs: shared_conn,
    )
    try:
        ok_a1, attempts_a1 = runner._register_rerun_attempt(
            "owner/repo", 57, "sha:a", 5
        )
        ok_a2, attempts_a2 = runner._register_rerun_attempt(
            "owner/repo", 57, "sha:a", 5
        )
        ok_b1, attempts_b1 = runner._register_rerun_attempt(
            "owner/repo", 57, "sha:b", 5
        )

        runner._mark_rerun_completed("owner/repo", 57, "sha:a")
        ok_a3, attempts_a3 = runner._register_rerun_attempt(
            "owner/repo", 57, "sha:a", 5
        )
        ok_b2, attempts_b2 = runner._register_rerun_attempt(
            "owner/repo", 57, "sha:b", 5
        )
    finally:
        runner.get_settings = original_get_settings
        shared_conn.close()

    assert (ok_a1, attempts_a1) == (True, 1)
    assert (ok_a2, attempts_a2) == (True, 2)
    assert (ok_b1, attempts_b1) == (True, 1)
    assert (ok_a3, attempts_a3) == (False, 2)
    assert (ok_b2, attempts_b2) == (True, 2)


def test_handle_review_job_skips_when_ci_pending(monkeypatch) -> None:
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner, "get_installation_token", lambda _iid: "tok")

    pr = SimpleNamespace(
        number=56,
        body="Closes #57",
        head=SimpleNamespace(sha="current-sha", ref="agent/issue-57"),
        title="PR",
    )
    issue = SimpleNamespace(number=57, title="Issue")
    repo = SimpleNamespace(
        get_pull=lambda _n: pr,
        get_issue=lambda _n: issue,
    )

    class _FakeGithub:
        def __init__(self, _token: str):
            pass

        def get_repo(self, _repo_full_name: str):
            return repo

    monkeypatch.setattr(runner, "Github", _FakeGithub)
    monkeypatch.setattr(
        runner,
        "_review_state_key",
        lambda _sha, _run_id: "sha:current-sha",
    )
    monkeypatch.setattr(runner, "_review_lock_key", lambda *_args: "lock")
    monkeypatch.setattr(runner, "_acquire_review_lock", lambda _k: True)

    released = {"ok": False}
    monkeypatch.setattr(
        runner, "_release_review_lock", lambda _k: released.__setitem__("ok", True)
    )

    called = {"register": False, "review": False}
    monkeypatch.setattr(
        runner,
        "_register_review_attempt",
        lambda *_args: called.__setitem__("register", True) or (False, 0),
    )
    monkeypatch.setattr(
        runner,
        "review_pull_request",
        lambda *_args, **_kwargs: called.__setitem__("review", True)
        or ("", False, None),
    )

    captured_head_sha: dict[str, str | None] = {"value": None}

    def _fake_get_runs(_repo, _pr, token=None, *, head_sha=None):
        _ = token
        captured_head_sha["value"] = head_sha
        return [SimpleNamespace(status="in_progress", conclusion=None)], {}

    monkeypatch.setattr(runner, "get_workflow_runs_and_logs", _fake_get_runs)

    result = runner.handle_review_job(
        "owner/repo",
        1,
        pr_number=56,
        head_sha="event-sha",
        run_id=123,
    )

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "ci_pending"
    assert released["ok"] is True
    assert called["register"] is False
    assert called["review"] is False
    assert captured_head_sha["value"] == "current-sha"


def test_handle_review_job_request_changes_non_agent_pr_does_not_rerun(
    monkeypatch,
) -> None:
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner, "get_installation_token", lambda _iid: "tok")

    pr = SimpleNamespace(
        number=56,
        body="Closes #57",
        head=SimpleNamespace(sha="head-sha", ref="feature/work"),
        title="PR",
    )
    issue = SimpleNamespace(number=57, title="Issue")
    repo = SimpleNamespace(
        get_pull=lambda _n: pr,
        get_issue=lambda _n: issue,
    )

    class _FakeGithub:
        def __init__(self, _token: str):
            pass

        def get_repo(self, _repo_full_name: str):
            return repo

    monkeypatch.setattr(runner, "Github", _FakeGithub)
    monkeypatch.setattr(
        runner,
        "_review_state_key",
        lambda _sha, _run_id: "sha:head-sha",
    )
    monkeypatch.setattr(runner, "_review_lock_key", lambda *_args: "lock")
    monkeypatch.setattr(runner, "_acquire_review_lock", lambda _k: True)
    monkeypatch.setattr(runner, "_release_review_lock", lambda _k: None)
    monkeypatch.setattr(runner, "_register_review_attempt", lambda *_args: (False, 1))
    monkeypatch.setattr(runner, "_mark_review_completed", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "get_workflow_runs_and_logs",
        lambda *_args, **_kwargs: (
            [SimpleNamespace(status="completed", conclusion="success")],
            {},
        ),
    )
    monkeypatch.setattr(
        runner,
        "review_pull_request",
        lambda *_args, **_kwargs: ("review", False, "request_changes"),
    )
    monkeypatch.setattr(
        runner, "create_pull_request_review", lambda *_args, **_kwargs: None
    )

    rerun_called = {"value": False}
    monkeypatch.setattr(
        runner,
        "_maybe_rerun_code_agent",
        lambda **_kwargs: rerun_called.__setitem__("value", True) or (True, 1),
    )

    result = runner.handle_review_job(
        "owner/repo",
        1,
        pr_number=56,
        head_sha="head-sha",
        run_id=321,
    )

    assert result["ok"] is True
    assert result["verdict"] == "request_changes"
    assert result["rerun_triggered"] is False
    assert rerun_called["value"] is False

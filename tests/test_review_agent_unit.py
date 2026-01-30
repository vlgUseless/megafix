from types import SimpleNamespace
from unittest.mock import patch

from reviewer_agent.actions_logs import get_workflow_runs_and_logs
from reviewer_agent.review_agent import review_pull_request


class FakeJob:
    def __init__(self, id_, name, conclusion, logs_url="https://example/logs"):
        self.id = id_
        self.name = name
        self.conclusion = conclusion
        self.logs_url = logs_url


class FakeRun:
    def __init__(self, name, conclusion, jobs):
        self.name = name
        self.conclusion = conclusion
        self._jobs = jobs

    def jobs(self):
        return self._jobs


class FakeRepo:
    def __init__(self, runs):
        self._runs = runs

    def get_workflow_runs(self, head_sha):
        return self._runs


def test_actions_logs_collects_failed_job_logs():
    pr = SimpleNamespace(head=SimpleNamespace(sha="abc"))
    runs = [
        FakeRun("CI", "success", []),
        FakeRun("CI", "failure", [FakeJob(1, "pytest", "failure")]),
    ]
    repo = FakeRepo(runs)

    with patch("reviewer_agent.actions_logs.requests.get") as rg:
        rg.return_value.status_code = 200
        rg.return_value.content = b"LOG CONTENT"
        rg.return_value.raise_for_status = lambda: None

        got_runs, failed_logs = get_workflow_runs_and_logs(repo, pr, token="t")
        assert len(got_runs) == 2
        assert failed_logs[1] == "LOG CONTENT"


def test_review_agent_stub_comment():
    pr = SimpleNamespace(
        number=1,
        title="Test PR",
        get_issue_comments=lambda: [1, 2],
        head=SimpleNamespace(sha="abc"),
    )
    issue = SimpleNamespace(number=10, title="Test issue")
    comment, approve = review_pull_request(
        pr, issue, workflow_runs=[], failed_job_logs={}
    )
    assert "stub implementation" in comment.lower()
    assert approve is False

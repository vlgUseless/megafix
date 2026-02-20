import json
from types import SimpleNamespace
from unittest.mock import patch

from megafix.infra.llm_clients import (
    LLMServiceError,
    ReviewFinding,
    StructuredReview,
    summarize_review,
)
from megafix.review_agent.actions_logs import get_workflow_runs_and_logs
from megafix.review_agent.application import review_pull_request
from megafix.shared.settings import get_settings


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

    class _Response:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=0):
            _ = chunk_size
            return [b"LOG CONTENT"]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("megafix.review_agent.actions_logs.requests.get") as rg:
        rg.return_value = _Response()

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
    with patch(
        "megafix.review_agent.application.summarize_review",
        side_effect=LLMServiceError("boom"),
    ):
        comment, approve, verdict, has_blocking_findings = review_pull_request(
            pr, issue, workflow_runs=[], failed_job_logs={}
        )
    assert "megafix review" in comment.lower()
    assert "llm review unavailable" in comment.lower()
    assert approve is False
    assert verdict is None
    assert has_blocking_findings is False


def test_review_agent_structured_request_changes_verdict():
    pr = SimpleNamespace(
        number=1,
        title="Test PR",
        get_issue_comments=lambda: [],
        get_files=lambda: [],
        head=SimpleNamespace(sha="abc"),
    )
    issue = SimpleNamespace(number=1, title="Test issue")
    structured = StructuredReview(
        summary=("API behavior changed.",),
        blocking_findings=(
            ReviewFinding(
                title="Missing backward compatibility note",
                details="README does not mention the breaking API change.",
                severity="medium",
                file="README.md",
                line=12,
            ),
        ),
        non_blocking_findings=(),
        tests=("CI passed.",),
        verdict="request_changes",
    )
    with patch(
        "megafix.review_agent.application.summarize_review",
        return_value=structured,
    ):
        comment, approve, verdict, has_blocking_findings = review_pull_request(
            pr,
            issue,
            workflow_runs=[SimpleNamespace(conclusion="success")],
            failed_job_logs={},
        )
    assert verdict == "request_changes"
    assert has_blocking_findings is True
    assert approve is False
    assert "**Verdict:** ❌ Request changes" in comment
    assert "- [MEDIUM] Missing backward compatibility note (README.md:12):" in comment
    assert comment.count("**Verdict:**") == 1


def _review_payload_text(verdict: str = "approve") -> str:
    return json.dumps(
        {
            "summary": ["ok"],
            "blocking_findings": [],
            "non_blocking_findings": [],
            "tests": ["CI passed"],
            "verdict": verdict,
        }
    )


def test_summarize_review_uses_default_llm_config_for_reviewer(monkeypatch):
    monkeypatch.setenv("LLM_SERVICE_URL", "https://default-llm.local")
    monkeypatch.setenv("LLM_SERVICE_API_KEY", "default-key")
    monkeypatch.setenv("LLM_SERVICE_MODEL", "default-model")
    monkeypatch.setenv("LLM_MAX_TOKENS", "123")
    # Keep REVIEW_* present-but-empty so local .env values do not leak into test.
    monkeypatch.setenv("REVIEW_LLM_SERVICE_URL", "")
    monkeypatch.setenv("REVIEW_LLM_SERVICE_API_KEY", "")
    monkeypatch.setenv("REVIEW_LLM_SERVICE_MODEL", "")
    monkeypatch.setenv("REVIEW_LLM_MAX_TOKENS", "")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": _review_payload_text()}}]}

    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response()

    with patch("megafix.infra.llm_clients.requests.post", side_effect=_fake_post):
        summary = summarize_review(
            "diff", {"runs": []}, {"number": 1, "title": "Issue"}
        )

    assert isinstance(summary, StructuredReview)
    assert summary.verdict == "approve"
    assert summary.summary == ("ok",)
    assert captured["url"] == "https://default-llm.local/v1/chat/completions"
    assert captured["json"]["model"] == "default-model"
    assert captured["json"]["max_tokens"] == 123
    assert captured["headers"]["Authorization"] == "Bearer default-key"
    get_settings.cache_clear()


def test_summarize_review_uses_reviewer_llm_overrides(monkeypatch):
    monkeypatch.setenv("LLM_SERVICE_URL", "https://default-llm.local")
    monkeypatch.setenv("LLM_SERVICE_API_KEY", "default-key")
    monkeypatch.setenv("LLM_SERVICE_MODEL", "default-model")
    monkeypatch.setenv("LLM_MAX_TOKENS", "123")
    monkeypatch.setenv("REVIEW_LLM_SERVICE_URL", "https://review-llm.local")
    monkeypatch.setenv("REVIEW_LLM_SERVICE_API_KEY", "review-key")
    monkeypatch.setenv("REVIEW_LLM_SERVICE_MODEL", "review-model")
    monkeypatch.setenv("REVIEW_LLM_MAX_TOKENS", "77")
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": _review_payload_text()}}]}

    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response()

    with patch("megafix.infra.llm_clients.requests.post", side_effect=_fake_post):
        summary = summarize_review(
            "diff", {"runs": []}, {"number": 1, "title": "Issue"}
        )

    assert isinstance(summary, StructuredReview)
    assert summary.verdict == "approve"
    assert captured["url"] == "https://review-llm.local/v1/chat/completions"
    assert captured["json"]["model"] == "review-model"
    assert captured["json"]["max_tokens"] == 77
    assert captured["headers"]["Authorization"] == "Bearer review-key"
    get_settings.cache_clear()

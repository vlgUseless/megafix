from __future__ import annotations

import importlib.util
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agent_core.agents.code_agent_base import IssueContext
from agent_core.orchestrator import run_issue_graph
from agent_core.settings import get_settings


class DummyMessage:
    def __init__(self, content: str, *, tool: bool = False):
        self.content = content
        if tool:
            self.tool_call_id = "tool_call"


def test_run_issue_graph_smoke(monkeypatch, tmp_path) -> None:
    def fake_run_patch_agent(llm, issue, repo_path):
        return {
            "messages": [
                DummyMessage("tool output", tool=True),
                DummyMessage("final summary"),
            ],
            "checks_ok": True,
            "iterations": 2,
        }

    monkeypatch.setattr(run_issue_graph, "_ensure_llm_settings", lambda _: None)
    monkeypatch.setattr(run_issue_graph, "_build_llm", lambda _: object())
    monkeypatch.setattr(run_issue_graph, "run_patch_agent", fake_run_patch_agent)

    settings = SimpleNamespace()
    issue = IssueContext(number=7, title="Fix", body="Details")
    result = run_issue_graph.run_issue_graph(issue, tmp_path, settings)
    assert result.final_message == "final summary"
    assert result.checks_ok is True
    assert result.iterations == 2


def test_run_issue_graph_integration(tmp_path, monkeypatch) -> None:
    pytest.importorskip("langgraph")
    pytest.importorskip("langchain_core")
    if shutil.which("git") is None:
        pytest.skip("git not available")
    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff not installed")
    # Keep integration deterministic: local .env commands must not affect checks.
    monkeypatch.delenv("AGENT_APPLY_CMD", raising=False)
    get_settings.cache_clear()

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_dummy.py").write_text(
        "def test_dummy():\n    assert True\n", encoding="utf-8"
    )

    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1,2 @@\n"
        " one\n"
        "+two\n"
    )

    class FakeMessage:
        def __init__(
            self, content: str, tool_calls: list[dict[str, object]] | None = None
        ):
            self.content = content
            self.tool_calls = tool_calls or []

    class FakeLLM:
        def __init__(self, responses: list[FakeMessage]):
            self._responses = responses

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if not self._responses:
                return FakeMessage("final", [])
            return self._responses.pop(0)

    responses = [
        FakeMessage(
            "",
            tool_calls=[
                {
                    "name": "repo_propose_patches",
                    "args": {"patches": [{"path": "hello.txt", "unified_diff": patch}]},
                    "id": "p1",
                }
            ],
        ),
        FakeMessage(
            "",
            tool_calls=[
                {
                    "name": "repo_apply_patches",
                    "args": {"patches": [{"path": "hello.txt", "unified_diff": patch}]},
                    "id": "a1",
                }
            ],
        ),
        FakeMessage("all good", tool_calls=[]),
    ]
    fake_llm = FakeLLM(responses)

    captured: dict[str, object] = {}
    from agent_core.orchestrator import langgraph_cycle as cycle

    def wrapped_run_patch_agent(llm, issue, repo_path):
        state = cycle.run_patch_agent(llm, issue, repo_path=repo_path)
        captured["state"] = state
        return state

    monkeypatch.setattr(run_issue_graph, "_build_llm", lambda _: fake_llm)
    monkeypatch.setattr(run_issue_graph, "run_patch_agent", wrapped_run_patch_agent)

    settings = SimpleNamespace(
        llm_service_url="http://fake",
        llm_service_api_key="test",
        llm_service_model="fake",
        llm_service_timeout_sec=1,
        llm_max_tokens=256,
    )
    issue = IssueContext(number=1, title="Demo", body="Test")
    result = run_issue_graph.run_issue_graph(issue, tmp_path, settings)

    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert result.checks_ok is True
    assert result.final_message == "all good"

    state = captured["state"]
    messages = state["messages"]
    tool_payloads = {}
    for message in messages:
        tool_call_id = getattr(message, "tool_call_id", None)
        if not tool_call_id:
            continue
        content = getattr(message, "content", "")
        tool_payloads[str(tool_call_id)] = content

    assert "p1" in tool_payloads
    assert "a1" in tool_payloads
    assert '"accepted": true' in tool_payloads["p1"].lower()
    assert '"applied": true' in tool_payloads["a1"].lower()
    assert any(
        "run_checks result" in (getattr(msg, "content", "") or "") for msg in messages
    )
    get_settings.cache_clear()

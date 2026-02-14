from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.agents.code_agent_base import IssueContext
from agent_core.orchestrator import langgraph_cycle as cycle

langgraph = pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")


class FakeMessage:
    def __init__(self, content: str, tool_calls: list[dict[str, object]] | None = None):
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


def test_langgraph_loop_checks_retry(monkeypatch, tmp_path):
    apply_calls = 0
    check_calls = 0

    def repo_apply_stub(args, repo_path=None):
        nonlocal apply_calls
        apply_calls += 1
        return {"applied": True, "errors": [], "stats": None}

    def run_checks_stub(args, repo_path=None):
        nonlocal check_calls
        check_calls += 1
        ok = check_calls > 1
        exit_code = 0 if ok else 1
        return {
            "ok": ok,
            "results": [
                {
                    "command": "pytest",
                    "exit_code": exit_code,
                    "stdout": "",
                    "stderr": "",
                }
            ],
        }

    def get_handler(name: str):
        if name == "repo_apply_patches":
            return repo_apply_stub
        if name == "run_checks":
            return run_checks_stub
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=4),
    )

    responses = [
        FakeMessage(
            "",
            tool_calls=[
                {
                    "name": "repo_apply_patches",
                    "args": {"patches": []},
                    "id": "1",
                }
            ],
        ),
        FakeMessage(
            "",
            tool_calls=[
                {
                    "name": "repo_apply_patches",
                    "args": {"patches": []},
                    "id": "2",
                }
            ],
        ),
        FakeMessage("final summary", tool_calls=[]),
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm, IssueContext(number=1, title="T", body=""), repo_path=tmp_path
    )
    assert apply_calls == 2
    assert check_calls == 2
    assert state["checks_ok"] is True
    assert state["force_final"] is True


def test_langgraph_limits_tool_calls(monkeypatch, tmp_path):
    calls: list[str] = []

    def repo_apply_stub(args, repo_path=None):
        calls.append("apply")
        return {"applied": True, "errors": [], "stats": None}

    def repo_propose_stub(args, repo_path=None):
        calls.append("propose")
        return {"accepted": True, "errors": [], "stats": None}

    def run_checks_stub(args, repo_path=None):
        return {
            "ok": True,
            "results": [
                {
                    "command": "pytest",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                }
            ],
        }

    def get_handler(name: str):
        if name == "repo_apply_patches":
            return repo_apply_stub
        if name == "repo_propose_patches":
            return repo_propose_stub
        if name == "run_checks":
            return run_checks_stub
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=1),
    )

    responses = [
        FakeMessage(
            "",
            tool_calls=[
                {"name": "repo_apply_patches", "args": {"patches": []}, "id": "1"},
                {"name": "repo_propose_patches", "args": {"patches": []}, "id": "2"},
            ],
        ),
        FakeMessage("final", tool_calls=[]),
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm, IssueContext(number=1, title="T", body=""), repo_path=tmp_path
    )
    assert calls == ["apply"]
    assert state["force_final"] is True
    message_texts = [
        text
        for text in (getattr(msg, "content", None) for msg in state["messages"])
        if isinstance(text, str)
    ]
    assert any("Too many tool calls" in text for text in message_texts)


def test_langgraph_stops_on_repeated_apply_errors(monkeypatch, tmp_path):
    apply_calls = 0

    def repo_apply_stub(args, repo_path=None):
        nonlocal apply_calls
        apply_calls += 1
        return {
            "applied": False,
            "errors": [{"code": "git_apply_check_failed", "message": "nope"}],
        }

    def get_handler(name: str):
        if name == "repo_apply_patches":
            return repo_apply_stub
        if name == "run_checks":
            return lambda *args, **kwargs: {"ok": True, "results": []}
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=2),
    )

    responses = [
        FakeMessage(
            "", tool_calls=[{"name": "repo_apply_patches", "args": {}, "id": "1"}]
        ),
        FakeMessage(
            "", tool_calls=[{"name": "repo_apply_patches", "args": {}, "id": "2"}]
        ),
        FakeMessage(
            "", tool_calls=[{"name": "repo_apply_patches", "args": {}, "id": "3"}]
        ),
        FakeMessage("final", tool_calls=[]),
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm, IssueContext(number=1, title="T", body=""), repo_path=tmp_path
    )
    assert apply_calls == 3
    assert state["force_final"] is True


def test_langgraph_stops_after_tool_turn_limit(monkeypatch, tmp_path):
    propose_calls = 0

    def repo_propose_stub(args, repo_path=None):
        nonlocal propose_calls
        propose_calls += 1
        return {"accepted": True, "errors": [], "stats": None}

    def get_handler(name: str):
        if name == "repo_propose_patches":
            return repo_propose_stub
        if name == "run_checks":
            return lambda *args, **kwargs: {"ok": True, "results": []}
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=4),
    )

    responses = [
        FakeMessage(
            "",
            tool_calls=[{"name": "repo_propose_patches", "args": {}, "id": str(i)}],
        )
        for i in range(7)
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm,
        IssueContext(number=1, title="T", body=""),
        repo_path=tmp_path,
        max_iterations=1,
    )
    assert propose_calls <= state["max_tool_turns"]
    assert state["force_final"] is True
    message_texts = [
        text
        for text in (getattr(msg, "content", None) for msg in state["messages"])
        if isinstance(text, str)
    ]
    assert any("Tool limit reached" in text for text in message_texts)


def test_langgraph_runs_checks_after_repo_apply_edits(monkeypatch, tmp_path):
    apply_calls = 0
    check_calls = 0

    def repo_apply_edits_stub(args, repo_path=None):
        nonlocal apply_calls
        apply_calls += 1
        return {"applied": True, "errors": [], "stats": None}

    def run_checks_stub(args, repo_path=None):
        nonlocal check_calls
        check_calls += 1
        return {
            "ok": True,
            "results": [
                {
                    "command": "pytest",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                }
            ],
        }

    def get_handler(name: str):
        if name == "repo_apply_edits":
            return repo_apply_edits_stub
        if name == "run_checks":
            return run_checks_stub
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=2),
    )

    responses = [
        FakeMessage(
            "",
            tool_calls=[{"name": "repo_apply_edits", "args": {"edits": []}, "id": "1"}],
        ),
        FakeMessage("final", tool_calls=[]),
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm, IssueContext(number=1, title="T", body=""), repo_path=tmp_path
    )
    assert apply_calls == 1
    assert check_calls == 1
    assert state["checks_ok"] is True


def test_langgraph_counts_repo_propose_edits_failures(monkeypatch, tmp_path):
    propose_calls = 0

    def repo_propose_edits_stub(args, repo_path=None):
        nonlocal propose_calls
        propose_calls += 1
        return {
            "accepted": False,
            "errors": [{"code": "policy_violation", "message": "too much delete"}],
            "stats": None,
            "patches": [],
        }

    def get_handler(name: str):
        if name == "repo_propose_edits":
            return repo_propose_edits_stub
        if name == "run_checks":
            return lambda *args, **kwargs: {"ok": True, "results": []}
        return lambda *args, **kwargs: {}

    monkeypatch.setattr(cycle, "get_tool_handler", get_handler)
    monkeypatch.setattr(cycle, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(
        cycle,
        "get_settings",
        lambda: SimpleNamespace(tool_max_calls_per_turn=2),
    )

    responses = [
        FakeMessage(
            "",
            tool_calls=[
                {"name": "repo_propose_edits", "args": {"edits": []}, "id": str(i)}
            ],
        )
        for i in range(5)
    ]
    llm = FakeLLM(responses)
    state = cycle.run_patch_agent(
        llm, IssueContext(number=1, title="T", body=""), repo_path=tmp_path
    )
    assert propose_calls == 4
    assert state["force_final"] is True

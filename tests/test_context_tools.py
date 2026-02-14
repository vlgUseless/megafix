from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core.settings import get_settings
from agent_core.tools.context_tools import repo_grep, repo_list_files, repo_read_file


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def _add_file(path: Path, rel: str, content: str) -> None:
    file_path = path / rel
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def test_repo_list_files(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    _add_file(tmp_path, "hello.txt", "hi\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=tmp_path, check=True)

    result = repo_list_files({}, repo_path=tmp_path)
    assert "hello.txt" in result


def test_repo_grep(tmp_path: Path, monkeypatch) -> None:
    _add_file(tmp_path, "notes.txt", "alpha\nbeta\nalpha\n")
    monkeypatch.setattr("agent_core.tools.context_tools.shutil.which", lambda _: None)
    payload = {"query": "alpha", "max_results": 1}
    matches = repo_grep(payload, repo_path=tmp_path)
    assert len(matches) == 1
    assert matches[0]["path"] == "notes.txt"
    assert matches[0]["line_no"] == 1


def test_repo_grep_uses_rg_json(monkeypatch, tmp_path: Path) -> None:
    _add_file(tmp_path, "src/app.py", "print('hello')\nprint('world')\n")

    def fake_run(cmd, *args, **kwargs):
        assert "--fixed-strings" in cmd
        assert "--json" in cmd

        class Result:
            returncode = 0
            stderr = ""
            stdout = (
                '{"type":"match","data":{"path":{"text":"src/app.py"},'
                '"line_number":1,"lines":{"text":"print(\'hello\')\\n"}}}\n'
            )

        return Result()

    monkeypatch.setattr("agent_core.tools.context_tools.shutil.which", lambda _: "rg")
    monkeypatch.setattr("agent_core.tools.context_tools.subprocess.run", fake_run)
    matches = repo_grep({"query": "hello"}, repo_path=tmp_path)
    assert matches == [{"path": "src/app.py", "line_no": 1, "line": "print('hello')"}]


def test_repo_grep_rejects_empty_query(tmp_path: Path) -> None:
    _add_file(tmp_path, "notes.txt", "alpha\n")
    with pytest.raises(ValueError):
        repo_grep({"query": ""}, repo_path=tmp_path)


def test_repo_grep_rejects_zero_max_results(tmp_path: Path) -> None:
    _add_file(tmp_path, "notes.txt", "alpha\n")
    with pytest.raises(ValueError):
        repo_grep({"query": "alpha", "max_results": 0}, repo_path=tmp_path)


def test_repo_read_file(tmp_path: Path) -> None:
    _add_file(tmp_path, "src/example.txt", "one\ntwo\nthree\n")
    payload = {"path": "src/example.txt", "start_line": 2, "end_line": 3}
    result = repo_read_file(payload, repo_path=tmp_path)
    assert result["path"] == "src/example.txt"
    assert result["content"] == "two\nthree\n"


def test_repo_read_file_rejects_absolute_path(tmp_path: Path) -> None:
    _add_file(tmp_path, "abs.txt", "hi\n")
    payload = {"path": str(tmp_path / "abs.txt"), "start_line": 1, "end_line": 1}
    with pytest.raises(ValueError):
        repo_read_file(payload, repo_path=tmp_path)


def test_repo_read_file_rejects_parent_path(tmp_path: Path) -> None:
    _add_file(tmp_path, "nested/file.txt", "hi\n")
    payload = {"path": "nested/../file.txt", "start_line": 1, "end_line": 1}
    with pytest.raises(ValueError):
        repo_read_file(payload, repo_path=tmp_path)


def test_repo_read_file_respects_max_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_MAX_READ_LINES", "2")
    get_settings.cache_clear()
    _add_file(tmp_path, "limits.txt", "one\ntwo\nthree\n")
    payload = {"path": "limits.txt", "start_line": 1, "end_line": 3}
    with pytest.raises(ValueError):
        repo_read_file(payload, repo_path=tmp_path)
    get_settings.cache_clear()

from __future__ import annotations

import subprocess
from pathlib import Path

from megafix.code_agent import check_tools
from megafix.shared.settings import get_settings


def test_run_checks_rejects_shell_operators(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHECK_ALLOW_CUSTOM_COMMANDS", "1")
    monkeypatch.setenv("CHECK_ALLOWLIST", "pytest && echo hi")
    get_settings.cache_clear()

    result = check_tools.run_checks(
        {"commands": ["pytest && echo hi"]}, repo_path=tmp_path
    )
    assert result["ok"] is False
    assert result["results"]
    assert "Shell operators are not allowed" in result["results"][0]["stderr"]
    get_settings.cache_clear()


def test_run_checks_ignores_custom_commands_when_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHECK_ALLOW_CUSTOM_COMMANDS", "0")
    monkeypatch.delenv("AGENT_APPLY_CMD", raising=False)
    get_settings.cache_clear()

    seen: list[str] = []

    def fake_run(repo_root: Path, command: str, *, timeout: float):
        seen.append(command)
        return check_tools.CheckCommandResult(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(check_tools, "_run_command", fake_run)
    result = check_tools.run_checks(
        {"commands": ["python -c \"print('hi')\""]},
        repo_path=tmp_path,
    )
    assert result["ok"] is True
    assert seen == []
    assert result["results"] == []
    get_settings.cache_clear()


def test_run_checks_timeout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHECK_ALLOW_CUSTOM_COMMANDS", "1")
    monkeypatch.setenv("CHECK_ALLOWLIST", 'python -c "import time\\n time.sleep(999)"')
    get_settings.cache_clear()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="python", timeout=0.01, output="out", stderr="err"
        )

    monkeypatch.setattr(check_tools, "_run_command", fake_run)
    result = check_tools.run_checks(
        {"commands": ['python -c "import time\\n time.sleep(999)"']},
        repo_path=tmp_path,
    )
    assert result["ok"] is False
    assert result["results"][0]["exit_code"] == 124
    get_settings.cache_clear()


def test_run_checks_treats_pytest_exit_code_5_as_success(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHECK_ALLOW_CUSTOM_COMMANDS", "1")
    monkeypatch.setenv("CHECK_ALLOWLIST", "python -m pytest -q,python -m ruff check .")
    get_settings.cache_clear()

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_placeholder.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )

    seen: list[str] = []

    def fake_run(repo_root: Path, command: str, *, timeout: float):
        seen.append(command)
        if command == "python -m pytest -q":
            return check_tools.CheckCommandResult(
                command=command,
                exit_code=5,
                stdout="no tests ran in 0.00s",
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
            )
        return check_tools.CheckCommandResult(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(check_tools, "_run_command", fake_run)
    result = check_tools.run_checks(
        {"commands": ["python -m pytest -q", "python -m ruff check ."]},
        repo_path=tmp_path,
    )

    assert result["ok"] is True
    assert seen == ["python -m pytest -q", "python -m ruff check ."]
    assert result["results"][0]["exit_code"] == 0
    assert "No tests were collected by pytest" in result["results"][0]["stdout"]
    get_settings.cache_clear()


def test_run_checks_defaults_are_empty_for_non_python_repo(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_APPLY_CMD", raising=False)
    get_settings.cache_clear()

    seen: list[str] = []

    def fake_run(repo_root: Path, command: str, *, timeout: float):
        seen.append(command)
        return check_tools.CheckCommandResult(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(check_tools, "_run_command", fake_run)
    result = check_tools.run_checks(repo_path=tmp_path)

    assert result["ok"] is True
    assert seen == []
    assert result["results"] == []
    get_settings.cache_clear()


def test_run_checks_defaults_detect_pytest_and_ruff(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_APPLY_CMD", raising=False)
    get_settings.cache_clear()

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "def test_sample():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    seen: list[str] = []

    def fake_run(repo_root: Path, command: str, *, timeout: float):
        seen.append(command)
        return check_tools.CheckCommandResult(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(check_tools, "_run_command", fake_run)
    result = check_tools.run_checks(repo_path=tmp_path)

    assert result["ok"] is True
    assert seen == ["python -m pytest -q", "python -m ruff check ."]
    get_settings.cache_clear()

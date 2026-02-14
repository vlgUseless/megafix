from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_core.settings import get_settings


@dataclass(frozen=True)
class CheckCommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


def run_checks(
    payload: dict[str, object] | None = None, *, repo_path: Path | None = None
) -> dict[str, object]:
    commands = _validate_payload(payload)
    repo_root = _resolve_repo_root(repo_path)

    results: list[CheckCommandResult] = []
    settings = get_settings()
    per_command_timeout = settings.check_timeout_sec
    total_timeout = settings.check_total_timeout_sec
    start_time = time.monotonic()
    for command in commands:
        skip_reason = _skip_reason(repo_root, command)
        if skip_reason:
            results.append(
                CheckCommandResult(
                    command=command,
                    exit_code=0,
                    stdout=skip_reason,
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            )
            continue
        elapsed = time.monotonic() - start_time
        remaining = (
            total_timeout - elapsed if total_timeout > 0 else per_command_timeout
        )
        if remaining <= 0:
            results.append(
                CheckCommandResult(
                    command=command,
                    exit_code=124,
                    stdout="",
                    stderr="Total checks timeout exceeded.",
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            )
            break
        timeout = (
            min(per_command_timeout, remaining)
            if per_command_timeout > 0
            else remaining
        )
        try:
            result = _run_command(repo_root, command, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stdout_text = _coerce_text(exc.stdout)
            stderr_text = _coerce_text(exc.stderr) or "Command timed out."
            stdout, stdout_truncated = _truncate(
                stdout_text, settings.check_max_log_chars
            )
            stderr, stderr_truncated = _truncate(
                stderr_text, settings.check_max_log_chars
            )
            result = CheckCommandResult(
                command=command,
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            result = CheckCommandResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr=str(exc),
                stdout_truncated=False,
                stderr_truncated=False,
            )
        result = _normalize_non_fatal_exit(result)
        results.append(result)
        if result.exit_code != 0:
            break

    ok = all(result.exit_code == 0 for result in results)
    return {
        "ok": ok,
        "results": [asdict(result) for result in results],
    }


def _validate_payload(payload: dict[str, object] | None) -> list[str]:
    settings = get_settings()
    if payload is None:
        return _default_commands(settings)
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object.")
    if not payload:
        return _default_commands(settings)

    allowed = {"commands"}
    extra = set(payload.keys()) - allowed
    if extra:
        raise ValueError(f"Invalid payload keys: {sorted(extra)}")

    commands = payload.get("commands")
    if commands is None:
        return _default_commands(settings)
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty array of strings.")
    cleaned: list[str] = []
    for item in commands:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Each command must be a non-empty string.")
        cleaned.append(item.strip())

    if not settings.check_allow_custom_commands:
        return _default_commands(settings)

    allowlist = _allowed_commands(settings)
    disallowed = [cmd for cmd in cleaned if cmd not in allowlist]
    if disallowed:
        raise ValueError(f"commands not in allowlist: {disallowed}")
    return cleaned


def _default_commands(settings) -> list[str]:
    commands = ["python -m pytest -q", "python -m ruff check ."]
    if settings.apply_cmd:
        commands.insert(0, settings.apply_cmd)
    return commands


def _allowed_commands(settings) -> list[str]:
    if settings.check_allowlist:
        return list(settings.check_allowlist)
    return _default_commands(settings)


def _skip_reason(repo_root: Path, command: str) -> str | None:
    if _is_pytest_command(command) and not _repo_has_pytest_targets(repo_root):
        return "Skipped pytest: no tests/config were detected in the repository."
    return None


def _resolve_repo_root(repo_path: Path | None) -> Path:
    repo_root = (repo_path or Path.cwd()).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        raise ValueError("Repository path is not a directory.")
    return repo_root


def _run_command(
    repo_root: Path, command: str, *, timeout: float
) -> CheckCommandResult:
    settings = get_settings()
    try:
        args = _normalize_command_args(_split_command(command))
    except ValueError as exc:
        return CheckCommandResult(
            command=command,
            exit_code=1,
            stdout="",
            stderr=str(exc),
            stdout_truncated=False,
            stderr_truncated=False,
        )
    try:
        result = subprocess.run(
            args,
            cwd=repo_root,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = _truncate(
            result.stdout, settings.check_max_log_chars
        )
        stderr, stderr_truncated = _truncate(
            result.stderr, settings.check_max_log_chars
        )
        return CheckCommandResult(
            command=command,
            exit_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = _coerce_text(exc.stdout)
        stderr_text = _coerce_text(exc.stderr) or "Command timed out."
        stdout, stdout_truncated = _truncate(stdout_text, settings.check_max_log_chars)
        stderr, stderr_truncated = _truncate(stderr_text, settings.check_max_log_chars)
        return CheckCommandResult(
            command=command,
            exit_code=124,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


def _split_command(command: str) -> list[str]:
    if not command.strip():
        raise ValueError("Command is empty.")
    if _contains_shell_operators(command):
        raise ValueError(
            "Shell operators are not allowed in check commands. "
            "Provide a single command without pipes or redirects."
        )
    args = shlex.split(command, posix=os.name != "nt")
    if not args:
        raise ValueError("Command is empty after parsing.")
    return args


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text.strip(), False
    return text[-max_chars:].strip(), True


def _contains_shell_operators(command: str) -> bool:
    operators = ["&&", "||", "|", ";", ">", "<"]
    return any(op in command for op in operators)


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_command_args(args: list[str]) -> list[str]:
    if not args:
        return args
    executable = Path(args[0]).stem.lower()
    if executable.startswith("python"):
        return [sys.executable, *args[1:]]
    return args


def _normalize_non_fatal_exit(result: CheckCommandResult) -> CheckCommandResult:
    if _is_pytest_command(result.command) and result.exit_code == 5:
        note = "No tests were collected by pytest; treating exit code 5 as success."
        stdout = result.stdout.strip()
        stdout = f"{stdout}\n{note}" if stdout else note
        return CheckCommandResult(
            command=result.command,
            exit_code=0,
            stdout=stdout,
            stderr=result.stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
        )
    return result


def _is_pytest_command(command: str) -> bool:
    try:
        args = _split_command(command)
    except ValueError:
        return False
    if not args:
        return False
    executable = Path(args[0]).stem.lower()
    if executable == "pytest":
        return True
    if executable.startswith("python") and len(args) >= 3:
        return args[1] == "-m" and args[2].lower() == "pytest"
    return False


def _repo_has_pytest_targets(repo_root: Path) -> bool:
    for name in ("pytest.ini",):
        if (repo_root / name).exists():
            return True
    if _file_contains(repo_root / "pyproject.toml", "[tool.pytest.ini_options]"):
        return True
    if _file_contains(repo_root / "setup.cfg", "[tool:pytest]"):
        return True
    if _file_contains(repo_root / "tox.ini", "[pytest]"):
        return True

    ignored = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
    }
    stack = [repo_root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.is_symlink():
                    continue
                if child.name in {"tests", "test"}:
                    return True
                if child.name in ignored:
                    continue
                stack.append(child)
                continue
            lower_name = child.name.lower()
            if lower_name.startswith("test_") and lower_name.endswith(".py"):
                return True
            if lower_name.endswith("_test.py"):
                return True
    return False


def _file_contains(path: Path, marker: str) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return marker in text


ToolHandler = Callable[..., object]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "run_checks": run_checks,
}

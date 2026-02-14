from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from agent_core.settings import get_settings


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_no: int
    line: str


def repo_list_files(
    payload: dict[str, object] | None = None, *, repo_path: Path | None = None
) -> list[str]:
    _require_empty_payload(payload)
    repo_root = _resolve_repo_root(repo_path)
    return _git_ls_files(repo_root)


def repo_grep(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> list[dict[str, object]]:
    query, glob, max_results = _validate_grep_payload(payload)
    repo_root = _resolve_repo_root(repo_path)
    matches = _rg_grep(repo_root, query, glob, max_results)
    return [match.__dict__ for match in matches]


def repo_read_file(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> dict[str, object]:
    path, start_line, end_line = _validate_read_payload(payload)
    repo_root = _resolve_repo_root(repo_path)
    rel_path = _normalize_rel_path(path)
    abs_path = _resolve_repo_path(repo_root, rel_path)
    if abs_path is None:
        raise ValueError("Path escapes repository root.")
    if not abs_path.exists():
        raise ValueError(f"File does not exist: {rel_path}")
    if not abs_path.is_file():
        raise ValueError(f"Path is not a file: {rel_path}")

    content = _read_lines_slice(abs_path, start_line, end_line)
    return {"path": rel_path, "content": content}


def _require_empty_payload(payload: dict[str, object] | None) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object.")
    if payload:
        raise ValueError("Payload must be an empty object.")


def _validate_grep_payload(payload: dict[str, object]) -> tuple[str, str | None, int]:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object.")
    _ensure_keys(payload, allowed={"query", "glob", "max_results"}, required={"query"})
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")
    query = query.strip()

    glob = payload.get("glob")
    if glob is not None:
        if not isinstance(glob, str) or not glob.strip():
            raise ValueError("glob must be a non-empty string when provided.")
        glob = glob.strip()

    max_results_raw = payload.get("max_results")
    if max_results_raw is None:
        max_results = 200
    else:
        if not isinstance(max_results_raw, int):
            raise ValueError("max_results must be an integer when provided.")
        if max_results_raw < 1:
            raise ValueError("max_results must be >= 1.")
        max_results = max_results_raw

    return query, glob, max_results


def _validate_read_payload(payload: dict[str, object]) -> tuple[str, int, int]:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object.")
    _ensure_keys(
        payload,
        allowed={"path", "start_line", "end_line"},
        required={"path", "start_line", "end_line"},
    )
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string.")
    start_line = payload.get("start_line")
    end_line = payload.get("end_line")
    if not isinstance(start_line, int) or start_line < 1:
        raise ValueError("start_line must be an integer >= 1.")
    if not isinstance(end_line, int) or end_line < 1:
        raise ValueError("end_line must be an integer >= 1.")
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line.")
    max_lines = get_settings().context_max_read_lines
    if max_lines > 0 and (end_line - start_line + 1) > max_lines:
        raise ValueError(
            f"Requested range exceeds max_lines ({max_lines}). "
            "Reduce start_line/end_line."
        )
    return path, start_line, end_line


def _ensure_keys(
    payload: dict[str, object], *, allowed: set[str], required: set[str]
) -> None:
    keys = set(payload.keys())
    missing = required - keys
    extra = keys - allowed
    if missing or extra:
        raise ValueError(
            "Invalid payload keys; " f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def _resolve_repo_root(repo_path: Path | None) -> Path:
    repo_root = (repo_path or Path.cwd()).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        raise ValueError("Repository path is not a directory.")
    return repo_root


def _git_ls_files(repo_root: Path) -> list[str]:
    cmd = ["git", "-C", str(repo_root), "ls-files"]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not available on PATH.") from exc
    output = result.stdout.splitlines()
    return [line.strip() for line in output if line.strip()]


def _rg_grep(
    repo_root: Path,
    query: str,
    glob: str | None,
    max_results: int,
) -> list[GrepMatch]:
    if shutil.which("rg") is None:
        return _python_grep(repo_root, query, glob, max_results)

    cmd = ["rg", "--fixed-strings", "--json", "--no-heading", "--line-number", query]
    if glob:
        cmd.extend(["-g", glob])
    result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "rg failed.")
    if result.returncode == 1:
        return []

    matches: list[GrepMatch] = []
    for line in result.stdout.splitlines():
        if len(matches) >= max_results:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        path_text = _normalize_output_path(data.get("path", {}).get("text"), repo_root)
        line_no = data.get("line_number")
        line_text = data.get("lines", {}).get("text", "")
        if not isinstance(path_text, str) or not path_text:
            continue
        if not isinstance(line_no, int):
            continue
        if not isinstance(line_text, str):
            line_text = str(line_text)
        matches.append(
            GrepMatch(path=path_text, line_no=line_no, line=line_text.rstrip("\n"))
        )
    return matches


def _python_grep(
    repo_root: Path,
    query: str,
    glob: str | None,
    max_results: int,
) -> list[GrepMatch]:
    files = _list_candidate_files(repo_root, glob)
    matches: list[GrepMatch] = []
    for rel_path in files:
        abs_path = repo_root / rel_path
        try:
            with abs_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if query in line:
                        matches.append(
                            GrepMatch(
                                path=rel_path,
                                line_no=line_no,
                                line=line.rstrip("\n"),
                            )
                        )
                        if len(matches) >= max_results:
                            return matches
        except OSError:
            continue
    return matches


def _list_candidate_files(repo_root: Path, glob: str | None) -> list[str]:
    try:
        files = _git_ls_files(repo_root)
    except Exception:
        files = _walk_files(repo_root)
    if glob:
        files = [path for path in files if fnmatch(path, glob)]
    return files


def _walk_files(repo_root: Path) -> list[str]:
    files: list[str] = []
    for path in repo_root.rglob("*"):
        if path.is_dir():
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        parts = rel.parts
        if ".git" in parts:
            continue
        files.append(_normalize_rel_path(str(rel)))
    return files


def _normalize_rel_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        raise ValueError("Path is empty.")
    if normalized.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("Absolute paths are not allowed.")
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Path contains invalid segments.")
    return "/".join(parts)


def _resolve_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    abs_path = (repo_root / rel_path).resolve()
    try:
        abs_path.relative_to(repo_root)
    except ValueError:
        return None
    return abs_path


def _normalize_output_path(path: object, repo_root: Path) -> str | None:
    if not isinstance(path, str):
        return None
    if not path:
        return None
    raw = path.replace("\\", "/")
    if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
        try:
            rel = Path(raw).resolve().relative_to(repo_root)
            return _normalize_rel_path(str(rel))
        except Exception:
            return None
    return _normalize_rel_path(raw)


def _read_lines_slice(path: Path, start_line: int, end_line: int) -> str:
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for idx, line in enumerate(handle, start=1):
            if idx < start_line:
                continue
            if idx > end_line:
                break
            lines.append(line)
    return "".join(lines)


ToolHandler = Callable[..., object]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "repo_list_files": repo_list_files,
    "repo_grep": repo_grep,
    "repo_read_file": repo_read_file,
}

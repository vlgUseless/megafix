from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from agent_core.agents.code_agent_base import CodeAgentResult, IssueContext
from agent_core.llm import FileChange, generate_file_changes
from agent_core.settings import get_settings

LOG = logging.getLogger(__name__)

_MAX_RELEVANT_FILES = 12
_MAX_FILE_BYTES = 50_000
_MAX_TREE_ENTRIES = 5_000


def run_issue(issue: IssueContext, repo_path: Path) -> CodeAgentResult:
    repo_context = _build_repo_context(issue, repo_path)
    changes = generate_file_changes(issue, repo_context)
    _apply_file_changes(repo_path, changes)
    _run_fast_checks(repo_path)
    return _build_pr_result(issue)


def _build_repo_context(issue: IssueContext, repo_path: Path) -> dict[str, object]:
    tree, total_count, truncated = _repo_tree(repo_path)
    relevant_files = _select_relevant_files(issue, tree)
    files_payload = _read_files(repo_path, relevant_files)
    context: dict[str, object] = {
        "repo_tree": tree,
        "files": files_payload,
    }
    if truncated:
        context["repo_tree_truncated"] = True
        context["repo_tree_total"] = total_count
    return context


def _repo_tree(repo_path: Path) -> tuple[list[str], int, bool]:
    cmd = ["git", "-C", str(repo_path), "ls-files"]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    total = len(files)
    if len(files) > _MAX_TREE_ENTRIES:
        return files[:_MAX_TREE_ENTRIES], total, True
    return files, total, False


def _select_relevant_files(issue: IssueContext, tree: Iterable[str]) -> list[str]:
    tree_list = list(tree)
    tree_set = set(tree_list)
    candidates = _extract_path_candidates(issue)
    selected: list[str] = []
    for candidate in candidates:
        if candidate in tree_set and candidate not in selected:
            selected.append(candidate)
            if len(selected) >= _MAX_RELEVANT_FILES:
                return selected

    basenames: dict[str, list[str]] = {}
    for path in tree_list:
        name = Path(path).name
        basenames.setdefault(name, []).append(path)

    for candidate in candidates:
        if "/" in candidate:
            continue
        for path in basenames.get(Path(candidate).name, []):
            if path not in selected:
                selected.append(path)
                if len(selected) >= _MAX_RELEVANT_FILES:
                    return selected

    if not selected and "README.md" in tree_set:
        selected.append("README.md")

    return selected


def _extract_path_candidates(issue: IssueContext) -> list[str]:
    text_parts = [issue.title or ""]
    if issue.body:
        text_parts.append(issue.body)
    text = "\n".join(text_parts)
    raw_candidates = re.findall(r"[\w./-]+\.[A-Za-z0-9]{1,8}", text)
    normalized: list[str] = []
    for candidate in raw_candidates:
        if "://" in candidate:
            continue
        candidate = candidate.replace("\\", "/").lstrip("./")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _read_files(repo_path: Path, paths: Iterable[str]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for rel_path in paths:
        file_path = repo_path / rel_path
        if not file_path.exists() or not file_path.is_file():
            continue
        try:
            if file_path.stat().st_size > _MAX_FILE_BYTES:
                continue
            payload[rel_path] = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            LOG.warning("Failed to read %s: %s", rel_path, exc)
    return payload


def _apply_file_changes(repo_path: Path, changes: Iterable[FileChange]) -> None:
    repo_root = repo_path.resolve()
    applied = 0
    for change in changes:
        rel_path = _normalize_rel_path(change.path)
        abs_path = (repo_root / rel_path).resolve()
        if not _is_within_repo(repo_root, abs_path):
            raise ValueError(f"Unsafe path outside repo: {change.path}")

        if change.action == "delete":
            if abs_path.exists():
                if abs_path.is_dir():
                    raise ValueError(f"Refusing to delete directory: {change.path}")
                abs_path.unlink()
                applied += 1
            else:
                LOG.warning("Delete requested for missing file: %s", change.path)
            continue

        if abs_path.exists() and abs_path.is_dir():
            raise ValueError(f"Cannot overwrite directory: {change.path}")

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        content = change.content or ""
        abs_path.write_text(content, encoding="utf-8")
        applied += 1

    if applied == 0:
        raise ValueError("LLM did not apply any file changes.")


def _run_fast_checks(repo_path: Path) -> None:
    settings = get_settings()
    if not settings.llm_check_cmd:
        return
    LOG.info("Running fast checks: %s", settings.llm_check_cmd)
    subprocess.run(
        settings.llm_check_cmd,
        cwd=str(repo_path),
        shell=True,
        check=True,
    )


def _build_pr_result(issue: IssueContext) -> CodeAgentResult:
    title = f"Fix issue {issue.number}: {issue.title}"
    body = f"Closes #{issue.number}\n\nAutomated changes by megafix agent."
    return CodeAgentResult(pr_title=title, pr_body=body)


def _normalize_rel_path(path: str) -> Path:
    normalized = path.replace("\\", "/").lstrip("./")
    return Path(normalized)


def _is_within_repo(repo_root: Path, target: Path) -> bool:
    try:
        target.relative_to(repo_root)
    except ValueError:
        return False
    return True

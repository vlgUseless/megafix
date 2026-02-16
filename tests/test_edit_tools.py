from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from megafix.code_agent.edit_tools import repo_apply_edits, repo_propose_edits
from megafix.shared.settings import get_settings


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def test_repo_propose_edits_accepts_valid_edit(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    payload = {
        "edits": [
            {
                "path": "hello.txt",
                "op": "replace_range",
                "start_line": 1,
                "end_line": 1,
                "new_text": "one\ntwo\n",
                "expected_old_text": "one\n",
            }
        ]
    }

    result = repo_propose_edits(payload, repo_path=tmp_path)

    assert result["accepted"] is True
    assert result["errors"] == []
    assert result["stats"] is not None
    assert result["patches"]
    assert len(result["operation_results"]) == 1
    assert result["operation_results"][0]["status"] == "validated"
    assert result["operation_results"][0]["error"] is None


def test_repo_apply_edits_applies_file_change(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    payload = {
        "edits": [
            {
                "path": "hello.txt",
                "op": "insert_after",
                "line": 1,
                "new_text": "two\n",
                "expected_old_text": "one\n",
            }
        ]
    }

    result = repo_apply_edits(payload, repo_path=tmp_path)

    assert result["applied"] is True
    assert result["errors"] == []
    assert len(result["operation_results"]) == 1
    assert result["operation_results"][0]["status"] == "applied"
    assert result["operation_results"][0]["error"] is None
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_repo_propose_edits_reports_policy_violation(
    tmp_path: Path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("PATCH_MAX_DELETED_RATIO", "0.3")
    get_settings.cache_clear()
    try:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("line\n", encoding="utf-8")
        payload = {
            "edits": [
                {
                    "path": "README.md",
                    "op": "delete_range",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_old_text": "line\n",
                }
            ]
        }

        result = repo_propose_edits(payload, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result["accepted"] is False
    assert result["errors"]
    assert result["errors"][0]["code"] == "policy_violation"
    assert result["operation_results"]
    assert result["operation_results"][0]["status"] == "error"
    assert result["operation_results"][0]["error"]["code"] == "policy_violation"


def test_repo_apply_edits_rejects_invalid_payload(tmp_path: Path) -> None:
    result = repo_apply_edits({}, repo_path=tmp_path)
    assert result["applied"] is False
    assert result["errors"]
    assert result["errors"][0]["code"] == "invalid_arguments"
    assert result["operation_results"] == []


def test_repo_apply_edits_reports_conflict(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    payload = {
        "edits": [
            {
                "path": "hello.txt",
                "op": "replace_range",
                "start_line": 1,
                "end_line": 1,
                "new_text": "ONE\n",
                "expected_old_text": "wrong\n",
            }
        ]
    }

    result = repo_apply_edits(payload, repo_path=tmp_path)

    assert result["applied"] is False
    assert result["errors"]
    assert result["errors"][0]["code"] == "context_conflict"
    assert result["operation_results"]
    assert result["operation_results"][0]["status"] == "error"
    assert result["operation_results"][0]["error"]["code"] == "context_conflict"


def test_repo_propose_edits_allows_readme_delete_when_ratio_relaxed(
    tmp_path: Path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("PATCH_MAX_DELETED_RATIO", "1.0")
    get_settings.cache_clear()
    try:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("line\n", encoding="utf-8")
        payload = {
            "edits": [
                {
                    "path": "README.md",
                    "op": "delete_range",
                    "start_line": 1,
                    "end_line": 1,
                    "expected_old_text": "line\n",
                }
            ]
        }
        result = repo_propose_edits(payload, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result["accepted"] is True
    assert result["errors"] == []
    assert result["operation_results"][0]["status"] == "validated"


def test_repo_propose_edits_allows_readme_expansion_with_default_ratio(
    tmp_path: Path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("PATCH_MAX_DELETED_RATIO", "0.3")
    get_settings.cache_clear()
    try:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("line\n", encoding="utf-8")
        payload = {
            "edits": [
                {
                    "path": "README.md",
                    "op": "replace_range",
                    "start_line": 1,
                    "end_line": 1,
                    "line": None,
                    "new_text": "line\n\n## Installation\nText\n",
                    "expected_old_text": "line\n",
                }
            ]
        }
        result = repo_propose_edits(payload, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result["accepted"] is True
    assert result["errors"] == []
    assert result["operation_results"][0]["status"] == "validated"


def test_repo_propose_edits_can_create_file_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("EDIT_ALLOW_CREATE_FILES", "1")
    get_settings.cache_clear()
    try:
        _init_repo(tmp_path)
        payload = {
            "edits": [
                {
                    "path": "app/main.py",
                    "op": "create_file",
                    "start_line": None,
                    "end_line": None,
                    "line": None,
                    "new_text": "print('ok')\n",
                    "expected_old_text": "",
                }
            ]
        }
        result = repo_propose_edits(payload, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result["accepted"] is True
    assert result["errors"] == []
    assert result["operation_results"][0]["status"] == "validated"
    assert result["patches"]
    assert "new file mode 100644" in result["patches"][0]["unified_diff"]


def test_repo_apply_edits_rejects_create_file_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("EDIT_ALLOW_CREATE_FILES", "0")
    get_settings.cache_clear()
    try:
        _init_repo(tmp_path)
        payload = {
            "edits": [
                {
                    "path": "app/main.py",
                    "op": "create_file",
                    "start_line": None,
                    "end_line": None,
                    "line": None,
                    "new_text": "print('ok')\n",
                    "expected_old_text": "",
                }
            ]
        }
        result = repo_apply_edits(payload, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result["applied"] is False
    assert result["errors"]
    assert result["errors"][0]["code"] == "policy_violation"
    assert result["operation_results"][0]["status"] == "error"
    assert not (tmp_path / "app/main.py").exists()

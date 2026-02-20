from __future__ import annotations

from pathlib import Path

from megafix.code_agent.edits_engine import (
    _build_unified_diff,
    apply_edits,
    check_edits,
)
from megafix.shared.settings import get_settings


def _write(path: Path, rel_path: str, content: str) -> None:
    file_path = path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def test_check_edits_replace_range_generates_patch_without_writing(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hello.txt", "one\ntwo\nthree\n")
    edits = [
        {
            "path": "hello.txt",
            "op": "replace_range",
            "start_line": 2,
            "end_line": 2,
            "new_text": "TWO\n",
            "expected_old_text": "two\n",
        }
    ]

    result = check_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is False
    assert not result.errors
    assert len(result.patches) == 1
    assert len(result.operation_results) == 1
    assert result.operation_results[0].status == "validated"
    assert result.operation_results[0].error is None
    assert result.patches[0].path == "hello.txt"
    assert "diff --git a/hello.txt b/hello.txt" in result.patches[0].unified_diff
    assert "-two" in result.patches[0].unified_diff
    assert "+TWO" in result.patches[0].unified_diff
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_apply_edits_supports_sequential_insert_and_delete(tmp_path: Path) -> None:
    _write(tmp_path, "seq.txt", "a\nb\nc\n")
    edits = [
        {
            "path": "seq.txt",
            "op": "insert_after",
            "line": 1,
            "new_text": "x\n",
            "expected_old_text": "a\n",
        },
        {
            "path": "seq.txt",
            "op": "delete_range",
            "start_line": 3,
            "end_line": 3,
            "expected_old_text": "b\n",
        },
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    assert len(result.patches) == 1
    assert [item.status for item in result.operation_results] == ["applied", "applied"]
    assert all(item.error is None for item in result.operation_results)
    assert (tmp_path / "seq.txt").read_text(encoding="utf-8") == "a\nx\nc\n"


def test_apply_edits_fails_on_expected_old_text_mismatch_and_keeps_file(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "guard.txt", "a\nb\nc\n")
    edits = [
        {
            "path": "guard.txt",
            "op": "replace_range",
            "start_line": 2,
            "end_line": 2,
            "new_text": "B\n",
            "expected_old_text": "b\n",
        },
        {
            "path": "guard.txt",
            "op": "replace_range",
            "start_line": 1,
            "end_line": 1,
            "new_text": "A\n",
            "expected_old_text": "wrong\n",
        },
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    assert result.applied is False
    assert result.errors
    assert result.errors[0].code == "context_conflict"
    assert result.errors[0].index == 1
    assert result.operation_results[0].status == "applied"
    assert result.operation_results[1].status == "error"
    assert result.operation_results[1].error is not None
    assert result.operation_results[1].error.code == "context_conflict"
    assert (tmp_path / "guard.txt").read_text(encoding="utf-8") == "a\nb\nc\n"


def test_check_edits_rejects_unsafe_path(tmp_path: Path) -> None:
    edits = [
        {
            "path": "../secret.txt",
            "op": "replace_range",
            "start_line": 1,
            "end_line": 1,
            "new_text": "x\n",
            "expected_old_text": "x\n",
        }
    ]

    result = check_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "invalid_arguments"
    assert result.operation_results[0].status == "error"


def test_check_edits_rejects_out_of_bounds_range(tmp_path: Path) -> None:
    _write(tmp_path, "range.txt", "line1\n")
    edits = [
        {
            "path": "range.txt",
            "op": "delete_range",
            "start_line": 1,
            "end_line": 2,
            "expected_old_text": "line1\nline2\n",
        }
    ]

    result = check_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    assert result.applied is False
    assert result.errors
    assert result.errors[0].code == "invalid_range"
    assert result.operation_results[0].status == "error"


def test_check_edits_reports_anchor_not_found(tmp_path: Path) -> None:
    _write(tmp_path, "anchor.txt", "line1\nline2\n")
    edits = [
        {
            "path": "anchor.txt",
            "op": "insert_after",
            "line": 1,
            "new_text": "new\n",
            "expected_old_text": "wrong\n",
        }
    ]

    result = check_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "anchor_not_found"
    assert result.operation_results[0].status == "error"
    assert result.operation_results[0].error is not None
    assert result.operation_results[0].error.code == "anchor_not_found"


def test_apply_edits_marks_later_operations_as_skipped_after_failure(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "skip.txt", "a\nb\nc\n")
    edits = [
        {
            "path": "skip.txt",
            "op": "replace_range",
            "start_line": 1,
            "end_line": 1,
            "new_text": "A\n",
            "expected_old_text": "a\n",
        },
        {
            "path": "skip.txt",
            "op": "replace_range",
            "start_line": 2,
            "end_line": 2,
            "new_text": "B\n",
            "expected_old_text": "WRONG\n",
        },
        {
            "path": "skip.txt",
            "op": "replace_range",
            "start_line": 3,
            "end_line": 3,
            "new_text": "C\n",
            "expected_old_text": "c\n",
        },
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    statuses = [item.status for item in result.operation_results]
    assert statuses == ["applied", "error", "skipped"]
    assert result.operation_results[2].error is not None
    assert result.operation_results[2].error.code == "skipped_previous_error"


def test_apply_edits_accepts_strict_nullable_contract_shape(tmp_path: Path) -> None:
    _write(tmp_path, "strict.txt", "a\nb\n")
    edits = [
        {
            "path": "strict.txt",
            "op": "insert_after",
            "start_line": None,
            "end_line": None,
            "line": 1,
            "new_text": "x\n",
            "expected_old_text": "a\n",
        }
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    assert result.operation_results[0].status == "applied"
    assert (tmp_path / "strict.txt").read_text(encoding="utf-8") == "a\nx\nb\n"


def test_apply_edits_insert_after_tolerates_non_null_range_fields(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "insert.txt", "a\nb\n")
    edits = [
        {
            "path": "insert.txt",
            "op": "insert_after",
            "start_line": 1,
            "end_line": 1,
            "line": 1,
            "new_text": "x\n",
            "expected_old_text": "a\n",
        }
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    assert result.operation_results[0].status == "applied"
    assert (tmp_path / "insert.txt").read_text(encoding="utf-8") == "a\nx\nb\n"


def test_apply_edits_rejects_create_file_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EDIT_ALLOW_CREATE_FILES", "0")
    get_settings.cache_clear()
    try:
        edits = [
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
        result = apply_edits(edits, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "policy_violation"
    assert result.operation_results[0].status == "error"
    assert not (tmp_path / "app/main.py").exists()


def test_apply_edits_can_create_new_file_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EDIT_ALLOW_CREATE_FILES", "1")
    get_settings.cache_clear()
    try:
        edits = [
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
        result = apply_edits(edits, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    assert result.operation_results[0].status == "applied"
    assert (tmp_path / "app/main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert result.patches
    assert "--- /dev/null" in result.patches[0].unified_diff
    assert "new file mode 100644" in result.patches[0].unified_diff


def test_apply_edits_rejects_create_file_for_existing_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EDIT_ALLOW_CREATE_FILES", "1")
    get_settings.cache_clear()
    try:
        _write(tmp_path, "app/main.py", "print('old')\n")
        edits = [
            {
                "path": "app/main.py",
                "op": "create_file",
                "start_line": None,
                "end_line": None,
                "line": None,
                "new_text": "print('new')\n",
                "expected_old_text": "",
            }
        ]
        result = apply_edits(edits, repo_path=tmp_path)
    finally:
        get_settings.cache_clear()

    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "file_already_exists"
    assert result.operation_results[0].status == "error"
    assert (tmp_path / "app/main.py").read_text(encoding="utf-8") == "print('old')\n"


def test_build_unified_diff_marks_missing_eof_newline() -> None:
    patch = _build_unified_diff(
        "README.md",
        "# README for test repo // ITMO megaschool",
        "# README for test repo // ITMO megaschool\n\n## Installation\nText\n",
    )

    assert "diff --git a/README.md b/README.md" in patch
    assert "\\ No newline at end of file" in patch


def test_apply_edits_tolerates_expected_text_without_trailing_newline(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    edits = [
        {
            "path": "main.py",
            "op": "replace_range",
            "start_line": 1,
            "end_line": 1,
            "new_text": "from fastapi import FastAPI\nfrom os import getenv\n",
            "expected_old_text": "from fastapi import FastAPI",
        }
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    updated = (tmp_path / "main.py").read_text(encoding="utf-8")
    assert "from os import getenv" in updated


def test_apply_edits_relocates_range_when_expected_block_is_unique(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "main.py", "a\nb\nc\n")
    edits = [
        {
            "path": "main.py",
            "op": "replace_range",
            "start_line": 3,
            "end_line": 3,
            "new_text": "B\n",
            "expected_old_text": "b\n",
        }
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is True
    assert result.applied is True
    assert not result.errors
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "a\nB\nc\n"


def test_apply_edits_does_not_relocate_when_expected_block_is_ambiguous(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "main.py", "x\nb\nb\n")
    edits = [
        {
            "path": "main.py",
            "op": "replace_range",
            "start_line": 1,
            "end_line": 1,
            "new_text": "X\n",
            "expected_old_text": "b\n",
        }
    ]

    result = apply_edits(edits, repo_path=tmp_path)

    assert result.ok is False
    assert result.applied is False
    assert result.errors
    assert result.errors[0].code == "context_conflict"
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "x\nb\nb\n"

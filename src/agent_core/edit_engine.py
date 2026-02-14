from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import unified_diff
from fnmatch import fnmatch
from pathlib import Path

from agent_core.settings import get_settings


@dataclass(frozen=True)
class EditError:
    code: str
    message: str
    path: str | None = None
    index: int | None = None
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class EditOperationResult:
    index: int
    path: str | None
    op: str | None
    status: str
    error: EditError | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "path": self.path,
            "op": self.op,
            "status": self.status,
            "error": _error_payload(self.error),
        }


@dataclass(frozen=True)
class FileEditPatch:
    path: str
    unified_diff: str


@dataclass(frozen=True)
class EditApplyResult:
    ok: bool
    applied: bool
    errors: list[EditError]
    patches: list[FileEditPatch]
    operation_results: list[EditOperationResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "errors": [_error_payload(item) for item in self.errors],
            "patches": [
                {"path": item.path, "unified_diff": item.unified_diff}
                for item in self.patches
            ],
            "operation_results": [item.to_dict() for item in self.operation_results],
        }


@dataclass(frozen=True)
class _StructuredEdit:
    index: int
    path: str
    op: str
    expected_old_text: str
    start_line: int | None = None
    end_line: int | None = None
    line: int | None = None
    new_text: str | None = None


_REQUIRED_KEYS_BY_OP: dict[str, set[str]] = {
    "replace_range": {
        "path",
        "op",
        "start_line",
        "end_line",
        "new_text",
        "expected_old_text",
    },
    "insert_after": {"path", "op", "line", "new_text", "expected_old_text"},
    "delete_range": {"path", "op", "start_line", "end_line", "expected_old_text"},
    "create_file": {"path", "op", "new_text", "expected_old_text"},
}
_COMMON_REQUIRED_KEYS: set[str] = {"path", "op", "expected_old_text"}
_ALLOWED_EDIT_KEYS: set[str] = {
    "path",
    "op",
    "start_line",
    "end_line",
    "line",
    "new_text",
    "expected_old_text",
}
_MAX_CREATE_FILES_PER_REQUEST = 10
_MAX_CREATE_FILE_CHARS = 200_000


def check_edits(
    edits: Iterable[Mapping[str, object]],
    *,
    repo_path: Path | None = None,
) -> EditApplyResult:
    return _run_edits(edits, repo_path=repo_path, write=False)


def apply_edits(
    edits: Iterable[Mapping[str, object]],
    *,
    repo_path: Path | None = None,
) -> EditApplyResult:
    return _run_edits(edits, repo_path=repo_path, write=True)


def _run_edits(
    edits: Iterable[Mapping[str, object]],
    *,
    repo_path: Path | None,
    write: bool,
) -> EditApplyResult:
    repo_root = (repo_path or Path.cwd()).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        return EditApplyResult(
            ok=False,
            applied=False,
            errors=[
                EditError(
                    code="invalid_repo",
                    message="Repository path is not a directory.",
                    details={"repo_path": str(repo_root)},
                )
            ],
            patches=[],
            operation_results=[],
        )

    edit_list, list_error = _materialize_edits(edits)
    if list_error is not None:
        return EditApplyResult(
            ok=False,
            applied=False,
            errors=[list_error],
            patches=[],
            operation_results=[],
        )

    operation_results = _init_operation_results(edit_list)

    parsed_edits, parse_errors = _parse_edits(edit_list)
    if parse_errors:
        for error in parse_errors:
            if error.index is None:
                continue
            _set_operation_result(operation_results, error.index, "error", error)
        _mark_pending_skipped(
            operation_results,
            "Skipped because at least one operation is invalid.",
        )
        return EditApplyResult(
            ok=False,
            applied=False,
            errors=parse_errors,
            patches=[],
            operation_results=operation_results,
        )

    settings = get_settings()
    path_order: list[str] = []
    original_by_path: dict[str, str] = {}
    buffers_by_path: dict[str, list[str]] = {}
    abs_path_by_rel: dict[str, Path] = {}
    existing_by_path: dict[str, bool] = {}
    prep_errors: list[EditError] = []

    edits_by_path: dict[str, list[_StructuredEdit]] = {}
    for edit in parsed_edits:
        edits_by_path.setdefault(edit.path, []).append(edit)

    create_paths = {edit.path for edit in parsed_edits if edit.op == "create_file"}
    if len(create_paths) > _MAX_CREATE_FILES_PER_REQUEST:
        prep_errors.extend(
            EditError(
                code="policy_violation",
                message="Too many files requested for creation in one operation.",
                path=edit.path,
                index=edit.index,
                details={
                    "create_files": len(create_paths),
                    "limit": _MAX_CREATE_FILES_PER_REQUEST,
                },
            )
            for edit in parsed_edits
            if edit.op == "create_file"
        )

    for path, path_edits in edits_by_path.items():
        has_create_op = any(edit.op == "create_file" for edit in path_edits)
        resolved = _resolve_repo_path(repo_root, path)
        if resolved is None:
            errors = _errors_for_path_edits(
                path_edits,
                code="invalid_path",
                message="Path escapes repository.",
            )
            prep_errors.extend(errors)
            continue
        if _is_denied_path(
            path, settings.patch_deny_prefixes, settings.patch_deny_globs
        ):
            errors = _errors_for_path_edits(
                path_edits,
                code="policy_violation",
                message="Path is denied by policy.",
                details={"path": path},
            )
            prep_errors.extend(errors)
            continue
        if not resolved.exists():
            if not has_create_op:
                errors = _errors_for_path_edits(
                    path_edits,
                    code="file_not_found",
                    message="Target file does not exist.",
                )
                prep_errors.extend(errors)
                continue
            if not settings.edit_allow_create_files:
                errors = _errors_for_path_edits(
                    path_edits,
                    code="policy_violation",
                    message="File creation is disabled by policy.",
                    details={"env": "EDIT_ALLOW_CREATE_FILES"},
                )
                prep_errors.extend(errors)
                continue
            path_order.append(path)
            original_by_path[path] = ""
            buffers_by_path[path] = []
            abs_path_by_rel[path] = resolved
            existing_by_path[path] = False
            continue
        if not resolved.is_file():
            errors = _errors_for_path_edits(
                path_edits,
                code="invalid_path",
                message="Target path is not a file.",
            )
            prep_errors.extend(errors)
            continue
        if has_create_op:
            errors = _errors_for_path_edits(
                path_edits,
                code="file_already_exists",
                message="create_file cannot target an existing file.",
            )
            prep_errors.extend(errors)
            continue
        try:
            content = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            errors = _errors_for_path_edits(
                path_edits,
                code="io_error",
                message="Failed to read file.",
                details={"error": str(exc)},
            )
            prep_errors.extend(errors)
            continue

        path_order.append(path)
        original_by_path[path] = content
        buffers_by_path[path] = content.splitlines(keepends=True)
        abs_path_by_rel[path] = resolved
        existing_by_path[path] = True

    if prep_errors:
        for error in prep_errors:
            if error.index is None:
                continue
            _set_operation_result(operation_results, error.index, "error", error)
        _mark_pending_skipped(
            operation_results,
            "Skipped because file preparation failed.",
        )
        return EditApplyResult(
            ok=False,
            applied=False,
            errors=prep_errors,
            patches=[],
            operation_results=operation_results,
        )

    runtime_errors: list[EditError] = []
    failed = False
    for edit in parsed_edits:
        current = operation_results[edit.index]
        if current.status != "pending":
            continue
        if failed:
            skipped_error = EditError(
                code="skipped_previous_error",
                message="Skipped because a previous operation failed.",
                path=edit.path,
                index=edit.index,
            )
            _set_operation_result(
                operation_results, edit.index, "skipped", skipped_error
            )
            continue

        lines = buffers_by_path.get(edit.path)
        if lines is None:
            error = EditError(
                code="invalid_path",
                message="Edit references an unresolved path.",
                path=edit.path,
                index=edit.index,
            )
            runtime_errors.append(error)
            _set_operation_result(operation_results, edit.index, "error", error)
            failed = True
            continue

        apply_error = _apply_single_edit(lines, edit)
        if apply_error is not None:
            runtime_errors.append(apply_error)
            _set_operation_result(operation_results, edit.index, "error", apply_error)
            failed = True
            continue

        success_status = "applied" if write else "validated"
        _set_operation_result(operation_results, edit.index, success_status, None)

    if runtime_errors:
        return EditApplyResult(
            ok=False,
            applied=False,
            errors=runtime_errors,
            patches=[],
            operation_results=operation_results,
        )

    updated_by_path: dict[str, str] = {
        path: "".join(lines) for path, lines in buffers_by_path.items()
    }
    changed_paths = [
        path
        for path in path_order
        if updated_by_path.get(path, "") != original_by_path.get(path, "")
    ]

    patches: list[FileEditPatch] = []
    for path in changed_paths:
        patch_text = _build_unified_diff(
            path,
            original_by_path[path],
            updated_by_path[path],
            old_exists=existing_by_path.get(path, True),
        )
        if patch_text:
            patches.append(FileEditPatch(path=path, unified_diff=patch_text))

    if write:
        for path in changed_paths:
            abs_path = abs_path_by_rel[path]
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(updated_by_path[path], encoding="utf-8")
            except OSError as exc:
                write_error = EditError(
                    code="io_error",
                    message="Failed to write file.",
                    path=path,
                    details={"error": str(exc)},
                )
                write_errors: list[EditError] = [write_error]
                for edit in edits_by_path.get(path, []):
                    op_error = EditError(
                        code="io_error",
                        message="Failed to write file.",
                        path=path,
                        index=edit.index,
                        details={"error": str(exc)},
                    )
                    write_errors.append(op_error)
                    _set_operation_result(
                        operation_results,
                        edit.index,
                        "error",
                        op_error,
                    )
                return EditApplyResult(
                    ok=False,
                    applied=False,
                    errors=write_errors,
                    patches=[],
                    operation_results=operation_results,
                )

    return EditApplyResult(
        ok=True,
        applied=bool(write and changed_paths),
        errors=[],
        patches=patches,
        operation_results=operation_results,
    )


def _materialize_edits(
    edits: Iterable[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], EditError | None]:
    if isinstance(edits, (str, bytes, bytearray, Mapping)):
        return [], EditError(
            code="invalid_arguments",
            message="edits must be an array of objects.",
        )
    try:
        items = list(edits)
    except TypeError:
        return [], EditError(
            code="invalid_arguments",
            message="edits must be an array of objects.",
        )
    if not items:
        return [], EditError(
            code="invalid_arguments",
            message="No edits provided.",
        )
    return items, None


def _parse_edits(
    edits: list[Mapping[str, object]],
) -> tuple[list[_StructuredEdit], list[EditError]]:
    parsed: list[_StructuredEdit] = []
    errors: list[EditError] = []
    for index, raw in enumerate(edits):
        if not isinstance(raw, Mapping):
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message="Each edit must be an object.",
                    index=index,
                )
            )
            continue

        op = raw.get("op")
        if not isinstance(op, str) or op not in _REQUIRED_KEYS_BY_OP:
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message="Unsupported edit op.",
                    index=index,
                    details={"op": op},
                )
            )
            continue

        keys = set(raw.keys())
        missing_required = sorted(_COMMON_REQUIRED_KEYS - keys)
        unknown = sorted(keys - _ALLOWED_EDIT_KEYS)
        if missing_required or unknown:
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message="Edit fields do not match op schema.",
                    index=index,
                    details={
                        "missing": missing_required,
                        "unknown": unknown,
                    },
                )
            )
            continue

        path = raw.get("path")
        normalized_path = _normalize_path(path if isinstance(path, str) else "")
        if not normalized_path or not _is_safe_path(normalized_path):
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message="Edit path must be a safe relative file path.",
                    index=index,
                    details={"path": path},
                )
            )
            continue

        expected_old_text = raw.get("expected_old_text")
        if not isinstance(expected_old_text, str):
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message="expected_old_text must be a string.",
                    index=index,
                    path=normalized_path,
                )
            )
            continue

        if op == "replace_range":
            start_line = _as_positive_int(raw.get("start_line"))
            end_line = _as_positive_int(raw.get("end_line"))
            line = raw.get("line")
            new_text = raw.get("new_text")
            if (
                start_line is None
                or end_line is None
                or line is not None
                or not isinstance(new_text, str)
                or start_line > end_line
            ):
                errors.append(
                    EditError(
                        code="invalid_arguments",
                        message=(
                            "replace_range requires start_line/end_line, new_text, "
                            "and line must be null."
                        ),
                        index=index,
                        path=normalized_path,
                    )
                )
                continue
            parsed.append(
                _StructuredEdit(
                    index=index,
                    path=normalized_path,
                    op=op,
                    start_line=start_line,
                    end_line=end_line,
                    new_text=new_text,
                    expected_old_text=expected_old_text,
                )
            )
            continue

        if op == "insert_after":
            line = _as_positive_int(raw.get("line"))
            start_line_raw = raw.get("start_line")
            end_line_raw = raw.get("end_line")
            new_text = raw.get("new_text")
            if (
                line is None
                or start_line_raw is not None
                or end_line_raw is not None
                or not isinstance(new_text, str)
            ):
                errors.append(
                    EditError(
                        code="invalid_arguments",
                        message=(
                            "insert_after requires line and new_text, and start_line/"
                            "end_line must be null."
                        ),
                        index=index,
                        path=normalized_path,
                    )
                )
                continue
            parsed.append(
                _StructuredEdit(
                    index=index,
                    path=normalized_path,
                    op=op,
                    line=line,
                    new_text=new_text,
                    expected_old_text=expected_old_text,
                )
            )
            continue

        if op == "create_file":
            start_line_raw = raw.get("start_line")
            end_line_raw = raw.get("end_line")
            line_raw = raw.get("line")
            new_text = raw.get("new_text")
            if (
                start_line_raw is not None
                or end_line_raw is not None
                or line_raw is not None
                or not isinstance(new_text, str)
                or not new_text
                or expected_old_text != ""
            ):
                errors.append(
                    EditError(
                        code="invalid_arguments",
                        message=(
                            'create_file requires new_text, expected_old_text="", '
                            "and start_line/end_line/line must be null."
                        ),
                        index=index,
                        path=normalized_path,
                    )
                )
                continue
            if len(new_text) > _MAX_CREATE_FILE_CHARS:
                errors.append(
                    EditError(
                        code="policy_violation",
                        message="Created file content exceeds size limit.",
                        index=index,
                        path=normalized_path,
                        details={
                            "chars": len(new_text),
                            "limit": _MAX_CREATE_FILE_CHARS,
                        },
                    )
                )
                continue
            parsed.append(
                _StructuredEdit(
                    index=index,
                    path=normalized_path,
                    op=op,
                    new_text=new_text,
                    expected_old_text=expected_old_text,
                )
            )
            continue

        start_line = _as_positive_int(raw.get("start_line"))
        end_line = _as_positive_int(raw.get("end_line"))
        line_raw = raw.get("line")
        new_text = raw.get("new_text")
        if (
            start_line is None
            or end_line is None
            or start_line > end_line
            or line_raw is not None
            or new_text is not None
        ):
            errors.append(
                EditError(
                    code="invalid_arguments",
                    message=(
                        "delete_range requires start_line/end_line, and line/new_text "
                        "must be null."
                    ),
                    index=index,
                    path=normalized_path,
                )
            )
            continue
        parsed.append(
            _StructuredEdit(
                index=index,
                path=normalized_path,
                op=op,
                start_line=start_line,
                end_line=end_line,
                expected_old_text=expected_old_text,
            )
        )
    return parsed, errors


def _apply_single_edit(lines: list[str], edit: _StructuredEdit) -> EditError | None:
    if edit.op == "replace_range":
        assert edit.start_line is not None
        assert edit.end_line is not None
        assert edit.new_text is not None
        range_error = _validate_range(lines, edit)
        if range_error is not None:
            return range_error
        start_idx = edit.start_line - 1
        end_idx = edit.end_line
        actual_text = "".join(lines[start_idx:end_idx])
        if actual_text != edit.expected_old_text:
            return _expected_text_mismatch(edit, actual_text)
        lines[start_idx:end_idx] = _text_to_lines(edit.new_text)
        return None

    if edit.op == "insert_after":
        assert edit.line is not None
        assert edit.new_text is not None
        if edit.line < 1 or edit.line > len(lines):
            return EditError(
                code="anchor_not_found",
                message="Anchor line is out of range.",
                path=edit.path,
                index=edit.index,
                details={"line": edit.line, "line_count": len(lines)},
            )
        anchor_idx = edit.line - 1
        actual_text = lines[anchor_idx]
        if actual_text != edit.expected_old_text:
            return _expected_text_mismatch(edit, actual_text)
        lines[anchor_idx + 1 : anchor_idx + 1] = _text_to_lines(edit.new_text)
        return None

    if edit.op == "create_file":
        assert edit.new_text is not None
        if lines:
            return EditError(
                code="file_already_exists",
                message="create_file cannot target an existing file.",
                path=edit.path,
                index=edit.index,
            )
        if edit.expected_old_text != "":
            return EditError(
                code="context_conflict",
                message='create_file requires expected_old_text to be "".',
                path=edit.path,
                index=edit.index,
                details={"expected_old_text": edit.expected_old_text},
            )
        lines[:] = _text_to_lines(edit.new_text)
        return None

    assert edit.start_line is not None
    assert edit.end_line is not None
    range_error = _validate_range(lines, edit)
    if range_error is not None:
        return range_error
    start_idx = edit.start_line - 1
    end_idx = edit.end_line
    actual_text = "".join(lines[start_idx:end_idx])
    if actual_text != edit.expected_old_text:
        return _expected_text_mismatch(edit, actual_text)
    del lines[start_idx:end_idx]
    return None


def _validate_range(lines: list[str], edit: _StructuredEdit) -> EditError | None:
    assert edit.start_line is not None
    assert edit.end_line is not None
    if edit.start_line < 1 or edit.end_line < 1:
        return EditError(
            code="invalid_range",
            message="Line numbers must be >= 1.",
            path=edit.path,
            index=edit.index,
            details={"start_line": edit.start_line, "end_line": edit.end_line},
        )
    if edit.start_line > edit.end_line:
        return EditError(
            code="invalid_range",
            message="start_line must be <= end_line.",
            path=edit.path,
            index=edit.index,
            details={"start_line": edit.start_line, "end_line": edit.end_line},
        )
    if edit.end_line > len(lines):
        return EditError(
            code="invalid_range",
            message="Line range is out of bounds.",
            path=edit.path,
            index=edit.index,
            details={
                "start_line": edit.start_line,
                "end_line": edit.end_line,
                "line_count": len(lines),
            },
        )
    return None


def _expected_text_mismatch(edit: _StructuredEdit, actual_text: str) -> EditError:
    if edit.op == "insert_after":
        return EditError(
            code="anchor_not_found",
            message="Anchor content mismatch at the specified line.",
            path=edit.path,
            index=edit.index,
            details={
                "op": edit.op,
                "expected_old_text": edit.expected_old_text,
                "actual_old_text": actual_text,
            },
        )
    return EditError(
        code="context_conflict",
        message="expected_old_text does not match current file content.",
        path=edit.path,
        index=edit.index,
        details={
            "op": edit.op,
            "expected_old_text": edit.expected_old_text,
            "actual_old_text": actual_text,
        },
    )


def _text_to_lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines(keepends=True)


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_safe_path(path: str) -> bool:
    if not path:
        return False
    if path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
        return False
    parts = [part for part in path.split("/") if part]
    return not any(part in {".", ".."} for part in parts)


def _is_denied_path(
    path: str,
    deny_prefixes: tuple[str, ...],
    deny_globs: tuple[str, ...],
) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for prefix in deny_prefixes:
        prefix_norm = prefix.replace("\\", "/")
        if prefix_norm.startswith("./"):
            prefix_norm = prefix_norm[2:]
        if not prefix_norm:
            continue
        if normalized == prefix_norm or normalized.startswith(
            prefix_norm.rstrip("/") + "/"
        ):
            return True
    return any(
        fnmatch(normalized, pattern.replace("\\", "/")) for pattern in deny_globs
    )


def _resolve_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    abs_path = (repo_root / rel_path).resolve()
    try:
        abs_path.relative_to(repo_root)
    except ValueError:
        return None
    return abs_path


def _build_unified_diff(
    path: str, old_text: str, new_text: str, *, old_exists: bool = True
) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    from_file = f"a/{path}" if old_exists else "/dev/null"
    diff_lines = list(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=from_file,
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if not diff_lines:
        return ""
    body = _render_unified_diff_lines(diff_lines)
    prefix = f"diff --git a/{path} b/{path}\n"
    if not old_exists:
        prefix += "new file mode 100644\n"
    return f"{prefix}{body}"


def _render_unified_diff_lines(diff_lines: list[str]) -> str:
    rendered: list[str] = []
    for line in diff_lines:
        if line.endswith("\n"):
            rendered.append(line)
            continue
        rendered.append(f"{line}\n")
        if _needs_no_newline_marker(line):
            rendered.append("\\ No newline at end of file\n")
    return "".join(rendered)


def _needs_no_newline_marker(line: str) -> bool:
    if line.startswith(("--- ", "+++ ")):
        return False
    return line.startswith((" ", "+", "-"))


def _init_operation_results(
    edits: list[Mapping[str, object]],
) -> list[EditOperationResult]:
    results: list[EditOperationResult] = []
    for index, raw in enumerate(edits):
        path: str | None = None
        op: str | None = None
        if isinstance(raw, Mapping):
            raw_path = raw.get("path")
            if isinstance(raw_path, str):
                path = _normalize_path(raw_path)
            raw_op = raw.get("op")
            if isinstance(raw_op, str):
                op = raw_op
        results.append(
            EditOperationResult(
                index=index,
                path=path,
                op=op,
                status="pending",
                error=None,
            )
        )
    return results


def _set_operation_result(
    results: list[EditOperationResult],
    index: int,
    status: str,
    error: EditError | None,
) -> None:
    current = results[index]
    results[index] = EditOperationResult(
        index=current.index,
        path=current.path,
        op=current.op,
        status=status,
        error=error,
    )


def _mark_pending_skipped(results: list[EditOperationResult], message: str) -> None:
    for item in list(results):
        if item.status != "pending":
            continue
        skip_error = EditError(
            code="skipped",
            message=message,
            path=item.path,
            index=item.index,
        )
        _set_operation_result(results, item.index, "skipped", skip_error)


def _errors_for_path_edits(
    edits: list[_StructuredEdit],
    *,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> list[EditError]:
    errors: list[EditError] = []
    for edit in edits:
        errors.append(
            EditError(
                code=code,
                message=message,
                path=edit.path,
                index=edit.index,
                details=details,
            )
        )
    return errors


def _error_payload(error: EditError | None) -> dict[str, object] | None:
    if error is None:
        return None
    details = dict(error.details or {})
    if error.index is not None:
        details.setdefault("index", error.index)
    return {
        "code": error.code,
        "message": error.message,
        "file_path": error.path,
        "line": None,
        "details": details or None,
    }

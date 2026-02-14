from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import cast

from agent_core.edit_engine import (
    EditError,
    EditOperationResult,
    apply_edits,
    check_edits,
)
from agent_core.patch_engine import PatchError, check_patches

_ALLOWED_KEYS = {"edits"}


def repo_propose_edits(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> dict[str, object]:
    edits, errors = _validate_payload(payload)
    if errors:
        return _result_dict(
            accepted=False,
            applied=False,
            errors=errors,
            stats=None,
            patches=[],
            operation_results=[],
            mode="propose",
        )

    preview = check_edits(edits, repo_path=repo_path)
    operation_results = list(preview.operation_results)
    if not preview.ok:
        return _result_dict(
            accepted=False,
            applied=False,
            errors=_normalize_edit_errors(preview.errors),
            stats=None,
            patches=[],
            operation_results=_operation_results_payload(operation_results),
            mode="propose",
        )

    diffs = [item.unified_diff for item in preview.patches]
    if not diffs:
        operation_results = _mark_no_change_operations(operation_results)
        return _result_dict(
            accepted=False,
            applied=False,
            errors=[_arg_error("Edits produced no file changes.", code="no_changes")],
            stats=None,
            patches=[],
            operation_results=_operation_results_payload(operation_results),
            mode="propose",
        )

    check_result = check_patches(diffs, repo_path=repo_path)
    if check_result.errors:
        operation_results = _attach_patch_errors(
            operation_results,
            check_result.errors,
        )
    return _result_dict(
        accepted=check_result.ok,
        applied=False,
        errors=[asdict(err) for err in check_result.errors],
        stats=asdict(check_result.stats) if check_result.stats is not None else None,
        patches=[asdict(item) for item in preview.patches],
        operation_results=_operation_results_payload(operation_results),
        mode="propose",
    )


def repo_apply_edits(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> dict[str, object]:
    edits, errors = _validate_payload(payload)
    if errors:
        return _result_dict(
            accepted=False,
            applied=False,
            errors=errors,
            stats=None,
            patches=[],
            operation_results=[],
            mode="apply",
        )

    preview = check_edits(edits, repo_path=repo_path)
    operation_results = list(preview.operation_results)
    if not preview.ok:
        return _result_dict(
            accepted=False,
            applied=False,
            errors=_normalize_edit_errors(preview.errors),
            stats=None,
            patches=[],
            operation_results=_operation_results_payload(operation_results),
            mode="apply",
        )

    diffs = [item.unified_diff for item in preview.patches]
    if not diffs:
        operation_results = _mark_no_change_operations(operation_results)
        return _result_dict(
            accepted=False,
            applied=False,
            errors=[_arg_error("Edits produced no file changes.", code="no_changes")],
            stats=None,
            patches=[],
            operation_results=_operation_results_payload(operation_results),
            mode="apply",
        )

    check_result = check_patches(diffs, repo_path=repo_path)
    if not check_result.ok:
        operation_results = _attach_patch_errors(
            operation_results,
            check_result.errors,
        )
        return _result_dict(
            accepted=False,
            applied=False,
            errors=[asdict(err) for err in check_result.errors],
            stats=None,
            patches=[asdict(item) for item in preview.patches],
            operation_results=_operation_results_payload(operation_results),
            mode="apply",
        )

    applied = apply_edits(edits, repo_path=repo_path)
    if not applied.ok:
        return _result_dict(
            accepted=False,
            applied=False,
            errors=_normalize_edit_errors(applied.errors),
            stats=None,
            patches=[],
            operation_results=_operation_results_payload(applied.operation_results),
            mode="apply",
        )

    return _result_dict(
        accepted=True,
        applied=applied.applied,
        errors=[],
        stats=asdict(check_result.stats) if check_result.stats is not None else None,
        patches=[asdict(item) for item in applied.patches],
        operation_results=_operation_results_payload(applied.operation_results),
        mode="apply",
    )


def _result_dict(
    *,
    accepted: bool,
    applied: bool,
    errors: list[dict[str, object]],
    stats: dict[str, object] | None,
    patches: list[dict[str, object]],
    operation_results: list[dict[str, object]],
    mode: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "errors": errors,
        "stats": stats,
        "patches": patches,
        "operation_results": operation_results,
    }
    if mode == "propose":
        payload["accepted"] = accepted
    else:
        payload["applied"] = applied
    return payload


def _validate_payload(
    payload: object,
) -> tuple[list[Mapping[str, object]], list[dict[str, object]]]:
    if not isinstance(payload, dict):
        return [], [_arg_error("Payload must be an object.")]

    keys = set(payload.keys())
    missing = _ALLOWED_KEYS - keys
    extra = keys - _ALLOWED_KEYS
    if missing or extra:
        return [], [
            _arg_error(
                "Payload must contain only the 'edits' field.",
                details={"missing": sorted(missing), "extra": sorted(extra)},
            )
        ]

    raw_edits = payload.get("edits")
    if not isinstance(raw_edits, list):
        return [], [_arg_error("'edits' must be an array.", field="edits")]
    if not raw_edits:
        return [], [_arg_error("'edits' must not be empty.", field="edits")]

    errors: list[dict[str, object]] = []
    for index, item in enumerate(raw_edits):
        if isinstance(item, Mapping):
            continue
        errors.append(
            _arg_error(
                "Each edit must be an object.",
                index=index,
                field="edits",
            )
        )
    if errors:
        return [], errors

    return cast(list[Mapping[str, object]], raw_edits), []


def _normalize_edit_errors(errors: list[EditError]) -> list[dict[str, object]]:
    return [
        {
            "code": error.code,
            "message": error.message,
            "file_path": error.path,
            "line": None,
            "details": _error_details(error),
        }
        for error in errors
    ]


def _operation_results_payload(
    operation_results: list[EditOperationResult],
) -> list[dict[str, object]]:
    return [item.to_dict() for item in operation_results]


def _attach_patch_errors(
    operation_results: list[EditOperationResult],
    patch_errors: list[PatchError],
) -> list[EditOperationResult]:
    updated = list(operation_results)
    for patch_error in patch_errors:
        target_indexes = _target_operation_indexes(updated, patch_error.file_path)
        for op_idx in target_indexes:
            current = updated[op_idx]
            details = dict(patch_error.details or {})
            details.setdefault("source", "patch_check")
            details.setdefault("patch_error_file_path", patch_error.file_path)
            mapped_error = EditError(
                code=patch_error.code,
                message=patch_error.message,
                path=current.path,
                index=current.index,
                details=details or None,
            )
            updated[op_idx] = EditOperationResult(
                index=current.index,
                path=current.path,
                op=current.op,
                status="error",
                error=mapped_error,
            )
    return updated


def _target_operation_indexes(
    operation_results: list[EditOperationResult], file_path: str | None
) -> list[int]:
    preferred = [
        idx
        for idx, item in enumerate(operation_results)
        if item.status in {"validated", "applied"}
        and file_path
        and item.path == file_path
    ]
    if preferred:
        return preferred
    return [
        idx
        for idx, item in enumerate(operation_results)
        if item.status in {"validated", "applied"}
    ]


def _mark_no_change_operations(
    operation_results: list[EditOperationResult],
) -> list[EditOperationResult]:
    updated = list(operation_results)
    for idx, item in enumerate(updated):
        if item.status not in {"validated", "applied"}:
            continue
        no_change_error = EditError(
            code="no_changes",
            message="Operation produced no net file change.",
            path=item.path,
            index=item.index,
        )
        updated[idx] = EditOperationResult(
            index=item.index,
            path=item.path,
            op=item.op,
            status="error",
            error=no_change_error,
        )
    return updated


def _error_details(error: EditError) -> dict[str, object] | None:
    details = dict(error.details or {})
    if error.index is not None:
        details.setdefault("index", error.index)
    return details or None


def _arg_error(
    message: str,
    *,
    code: str = "invalid_arguments",
    index: int | None = None,
    field: str | None = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    info: dict[str, object] = {}
    if index is not None:
        info["index"] = index
    if field is not None:
        info["field"] = field
    if details:
        info.update(details)
    return {
        "code": code,
        "message": message,
        "file_path": None,
        "line": None,
        "details": info or None,
    }


ToolHandler = Callable[..., object]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "repo_propose_edits": repo_propose_edits,
    "repo_apply_edits": repo_apply_edits,
}

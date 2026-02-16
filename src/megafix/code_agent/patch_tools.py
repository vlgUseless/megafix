from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from megafix.code_agent.patches_engine import (
    PatchApplyResult,
    PatchError,
    apply_patches,
    check_patches,
)

LOG = logging.getLogger(__name__)

_ALLOWED_PATCH_KEYS = {"patches"}
_ALLOWED_PATCH_ITEM_KEYS = {"path", "unified_diff"}


@dataclass(frozen=True)
class PatchInputItem:
    path: str
    unified_diff: str


def repo_propose_patches(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> dict[str, object]:
    patches, errors = _validate_payload(payload)
    if errors:
        result = PatchApplyResult(ok=False, applied=False, errors=errors, stats=None)
        return _result_dict(result, mode="propose")

    diffs = [item.unified_diff for item in patches]
    result = check_patches(diffs, repo_path=repo_path)
    return _result_dict(result, mode="propose")


def repo_apply_patches(
    payload: dict[str, object], *, repo_path: Path | None = None
) -> dict[str, object]:
    patches, errors = _validate_payload(payload)
    if errors:
        result = PatchApplyResult(ok=False, applied=False, errors=errors, stats=None)
        return _result_dict(result, mode="apply")

    diffs = [item.unified_diff for item in patches]
    result = apply_patches(diffs, repo_path=repo_path)
    return _result_dict(result, mode="apply")


def _result_dict(result: PatchApplyResult, *, mode: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "errors": [asdict(err) for err in result.errors],
        "stats": asdict(result.stats) if result.stats is not None else None,
    }
    if mode == "propose":
        payload["accepted"] = result.ok
    else:
        payload["applied"] = result.applied
    return payload


def _validate_payload(
    payload: object,
) -> tuple[list[PatchInputItem], list[PatchError]]:
    errors: list[PatchError] = []
    if not isinstance(payload, dict):
        return [], [_arg_error("Payload must be an object.")]

    keys = set(payload.keys())
    missing = _ALLOWED_PATCH_KEYS - keys
    extra = keys - _ALLOWED_PATCH_KEYS
    if missing or extra:
        message = "Payload must contain only the 'patches' field."
        details = {"missing": sorted(missing), "extra": sorted(extra)}
        errors.append(_arg_error(message, details=details))
        return [], errors

    raw_patches = payload.get("patches")
    if not isinstance(raw_patches, list):
        return [], [_arg_error("'patches' must be an array.", field="patches")]
    if not raw_patches:
        return [], [_arg_error("'patches' must not be empty.", field="patches")]

    items: list[PatchInputItem] = []
    for index, item in enumerate(raw_patches):
        if not isinstance(item, dict):
            errors.append(
                _arg_error(
                    "Each patch must be an object.", index=index, field="patches"
                )
            )
            continue
        item_keys = set(item.keys())
        missing_keys = _ALLOWED_PATCH_ITEM_KEYS - item_keys
        extra_keys = item_keys - _ALLOWED_PATCH_ITEM_KEYS
        if missing_keys or extra_keys:
            errors.append(
                _arg_error(
                    "Patch item must include only 'path' and 'unified_diff'.",
                    index=index,
                    details={
                        "missing": sorted(missing_keys),
                        "extra": sorted(extra_keys),
                    },
                )
            )
            continue

        path = item.get("path")
        diff = item.get("unified_diff")
        if not isinstance(path, str) or not path.strip():
            errors.append(
                _arg_error(
                    "Patch path must be a non-empty string.",
                    index=index,
                    field="path",
                )
            )
            continue
        if not _is_safe_path(path):
            errors.append(
                _arg_error(
                    "Patch path must be a relative path without '..' segments.",
                    index=index,
                    field="path",
                )
            )
            continue
        if not isinstance(diff, str) or not diff.strip():
            errors.append(
                _arg_error(
                    "unified_diff must be a non-empty string.",
                    index=index,
                    field="unified_diff",
                )
            )
            continue

        diff = _sanitize_unified_diff(diff)
        if not diff.strip():
            errors.append(
                _arg_error(
                    "unified_diff must contain a unified diff.",
                    index=index,
                    field="unified_diff",
                )
            )
            continue

        normalized_path = path.strip()
        diff_paths = _extract_diff_paths(diff)
        if diff_paths and normalized_path not in diff_paths:
            errors.append(
                _arg_error(
                    "Patch path does not match any file in unified_diff.",
                    index=index,
                    field="path",
                    details={"diff_paths": sorted(diff_paths)},
                )
            )
            continue

        items.append(PatchInputItem(path=normalized_path, unified_diff=diff))

    return items, errors


def _arg_error(
    message: str,
    *,
    index: int | None = None,
    field: str | None = None,
    details: Mapping[str, object] | None = None,
) -> PatchError:
    info: dict[str, object] = {}
    if index is not None:
        info["index"] = index
    if field is not None:
        info["field"] = field
    if details:
        info.update(details)
    return PatchError(
        code="invalid_arguments",
        message=message,
        details=info or None,
    )


def _is_safe_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return False
    if normalized.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = [part for part in normalized.split("/") if part]
    return not any(part in {".", ".."} for part in parts)


def _extract_diff_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        raw = line[4:].strip()
        if not raw or raw == "/dev/null":
            continue
        token = raw.split()[0]
        token = token.replace("\\", "/")
        if token.startswith(("a/", "b/")):
            token = token[2:]
        if token.startswith("./"):
            token = token[2:]
        if not token:
            continue
        paths.add(token)
    return paths


def _sanitize_unified_diff(diff: str) -> str:
    text = diff.strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].lstrip().startswith("```"):
        lines = lines[:-1]
    while lines and not lines[0].strip():
        lines = lines[1:]
    if not lines:
        return ""
    start = 0
    for idx, line in enumerate(lines):
        if _looks_like_patch_start(line):
            start = idx
            break
    else:
        LOG.debug("Unified diff has no patch start markers.")
        return text
    lines = lines[start:]
    end = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        if _looks_like_patch_line(lines[idx]):
            end = idx + 1
            break
    lines = lines[:end]
    if not lines:
        return ""
    return "\n".join(lines).strip() + "\n"


def _looks_like_patch_start(line: str) -> bool:
    return line.startswith(("diff --git ", "--- ", "+++ ", "@@ "))


def _looks_like_patch_line(line: str) -> bool:
    if line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")):
        return True
    if line.startswith((" ", "+", "-", "\\ No newline at end of file")):
        return True
    return _looks_like_metadata_line(line)


def _looks_like_metadata_line(line: str) -> bool:
    return line.startswith(
        (
            "index ",
            "new file mode ",
            "deleted file mode ",
            "similarity index ",
            "rename from ",
            "rename to ",
            "old mode ",
            "new mode ",
            "copy from ",
            "copy to ",
        )
    )


ToolHandler = Callable[..., object]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "repo_propose_patches": repo_propose_patches,
    "repo_apply_patches": repo_apply_patches,
}

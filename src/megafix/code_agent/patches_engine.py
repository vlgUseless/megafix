from __future__ import annotations

import logging
import re
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from megafix.shared.settings import get_settings

LOG = logging.getLogger(__name__)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_MARKERS = ("GIT binary patch", "Binary files ")
_HUNK_LINE_PREFIXES = (" ", "+", "-")
_NO_NEWLINE_MARKER = r"\ No newline at end of file"


@dataclass(frozen=True)
class PatchError:
    code: str
    message: str
    file_path: str | None = None
    line: int | None = None
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class PatchFileStats:
    path: str
    status: str
    additions: int
    deletions: int
    deletion_ratio: float | None


@dataclass(frozen=True)
class PatchStats:
    total_additions: int
    total_deletions: int
    files_touched: int
    per_file: list[PatchFileStats]


@dataclass(frozen=True)
class PatchApplyResult:
    ok: bool
    applied: bool
    errors: list[PatchError]
    stats: PatchStats | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return data


@dataclass(frozen=True)
class PatchPolicy:
    require_git_diff_header: bool
    max_files: int
    max_deletions_per_file: int
    max_deletion_ratio: float
    deny_prefixes: tuple[str, ...]
    deny_globs: tuple[str, ...]

    @classmethod
    def from_settings(cls) -> PatchPolicy:
        settings = get_settings()
        return cls(
            require_git_diff_header=settings.patch_require_git_diff_header,
            max_files=settings.patch_max_files,
            max_deletions_per_file=settings.patch_max_deleted_lines,
            max_deletion_ratio=settings.patch_max_deleted_ratio,
            deny_prefixes=settings.patch_deny_prefixes,
            deny_globs=settings.patch_deny_globs,
        )


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_count_explicit: bool = False
    new_count_explicit: bool = False
    additions: int = 0
    deletions: int = 0
    old_seen: int = 0
    new_seen: int = 0


@dataclass
class _FilePatch:
    header_old: str | None = None
    header_new: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    saw_old_header: bool = False
    saw_new_header: bool = False
    hunks: list[_Hunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0


def apply_patches(
    patches: Iterable[str],
    *,
    repo_path: Path | None = None,
    policy: PatchPolicy | None = None,
) -> PatchApplyResult:
    prepared, error_result = _prepare_patches(patches, repo_path, policy)
    if error_result is not None:
        return error_result
    if prepared is None:
        return PatchApplyResult(
            ok=False,
            applied=False,
            errors=[
                PatchError(code="invalid_patch", message="Patch preparation failed.")
            ],
        )

    check_error = _run_git_apply(
        prepared.repo_root, prepared.patch_text, check_only=True
    )
    if check_error:
        return PatchApplyResult(ok=False, applied=False, errors=[check_error])

    apply_error = _run_git_apply(
        prepared.repo_root, prepared.patch_text, check_only=False
    )
    if apply_error:
        return PatchApplyResult(ok=False, applied=False, errors=[apply_error])

    stats = _build_stats(prepared.file_stats)
    return PatchApplyResult(ok=True, applied=True, errors=[], stats=stats)


def check_patches(
    patches: Iterable[str],
    *,
    repo_path: Path | None = None,
    policy: PatchPolicy | None = None,
) -> PatchApplyResult:
    prepared, error_result = _prepare_patches(patches, repo_path, policy)
    if error_result is not None:
        return error_result
    if prepared is None:
        return PatchApplyResult(
            ok=False,
            applied=False,
            errors=[
                PatchError(code="invalid_patch", message="Patch preparation failed.")
            ],
        )

    check_error = _run_git_apply(
        prepared.repo_root, prepared.patch_text, check_only=True
    )
    if check_error:
        return PatchApplyResult(ok=False, applied=False, errors=[check_error])

    stats = _build_stats(prepared.file_stats)
    return PatchApplyResult(ok=True, applied=False, errors=[], stats=stats)


@dataclass
class _ParseResult:
    files: list[_FilePatch]
    errors: list[PatchError]


@dataclass(frozen=True)
class _PreparedPatches:
    repo_root: Path
    patch_text: str
    file_stats: list[PatchFileStats]


def _prepare_patches(
    patches: Iterable[str],
    repo_path: Path | None,
    policy: PatchPolicy | None,
) -> tuple[_PreparedPatches | None, PatchApplyResult | None]:
    patch_list = [patch for patch in patches if patch and patch.strip()]
    if not patch_list:
        return (
            None,
            PatchApplyResult(
                ok=False,
                applied=False,
                errors=[
                    PatchError(code="invalid_patch", message="No patches provided.")
                ],
            ),
        )

    repo_root = (repo_path or Path.cwd()).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        return (
            None,
            PatchApplyResult(
                ok=False,
                applied=False,
                errors=[
                    PatchError(
                        code="invalid_repo",
                        message="Repository path is not a directory.",
                        details={"repo_path": str(repo_root)},
                    )
                ],
            ),
        )

    policy = policy or PatchPolicy.from_settings()
    parse_result = _parse_patches(patch_list, policy.require_git_diff_header)
    if parse_result.errors:
        return (
            None,
            PatchApplyResult(ok=False, applied=False, errors=parse_result.errors),
        )

    deny_errors = _check_denied_paths(parse_result.files, policy)
    if deny_errors:
        return (None, PatchApplyResult(ok=False, applied=False, errors=deny_errors))

    file_stats, stat_errors = _compute_file_stats(parse_result.files, repo_root)
    if stat_errors:
        return (None, PatchApplyResult(ok=False, applied=False, errors=stat_errors))

    policy_errors = _check_policy(parse_result.files, file_stats, policy)
    if policy_errors:
        return (None, PatchApplyResult(ok=False, applied=False, errors=policy_errors))

    patch_text = _join_patches(patch_list)
    prepared = _PreparedPatches(
        repo_root=repo_root, patch_text=patch_text, file_stats=file_stats
    )
    return prepared, None


def _parse_patches(
    patches: Sequence[str], require_git_diff_header: bool
) -> _ParseResult:
    files: list[_FilePatch] = []
    for idx, patch in enumerate(patches):
        result = _parse_unified_diff(patch, require_git_diff_header, patch_index=idx)
        if result.errors:
            return result
        files.extend(result.files)
    if not files:
        return _ParseResult(
            files=[],
            errors=[
                PatchError(code="invalid_patch", message="Patch contains no files.")
            ],
        )
    return _ParseResult(files=files, errors=[])


def _parse_unified_diff(
    text: str, require_git_diff_header: bool, *, patch_index: int
) -> _ParseResult:
    if "\x00" in text:
        return _ParseResult(
            files=[],
            errors=[
                PatchError(
                    code="invalid_patch",
                    message="Patch contains NUL bytes (binary data).",
                    details={"patch_index": patch_index},
                )
            ],
        )

    for marker in _BINARY_MARKERS:
        if marker in text:
            return _ParseResult(
                files=[],
                errors=[
                    PatchError(
                        code="invalid_patch",
                        message="Binary patches are not allowed.",
                        details={"patch_index": patch_index, "marker": marker},
                    )
                ],
            )

    lines = text.splitlines()
    files: list[_FilePatch] = []
    current_file: _FilePatch | None = None
    current_hunk: _Hunk | None = None
    saw_diff_header = False
    saw_any_hunk = False

    def finalize_hunk(line_no: int) -> PatchError | None:
        nonlocal current_hunk
        if current_hunk is None:
            return None
        mismatch_old = (
            current_hunk.old_count_explicit
            and current_hunk.old_seen != current_hunk.old_count
        )
        mismatch_new = (
            current_hunk.new_count_explicit
            and current_hunk.new_seen != current_hunk.new_count
        )
        if mismatch_old or mismatch_new:
            LOG.debug(
                "Hunk line counts mismatch ignored (expected -%s/+%s, got -%s/+%s).",
                current_hunk.old_count,
                current_hunk.new_count,
                current_hunk.old_seen,
                current_hunk.new_seen,
            )
        current_hunk = None
        return None

    def finalize_file(line_no: int) -> PatchError | None:
        if current_file is None:
            return None
        if current_hunk is not None:
            error = finalize_hunk(line_no)
            if error:
                return error
        if not current_file.saw_old_header or not current_file.saw_new_header:
            return PatchError(
                code="invalid_patch",
                message="Missing ---/+++ file headers.",
                line=line_no,
                details={"patch_index": patch_index},
            )
        if not current_file.hunks:
            return PatchError(
                code="invalid_patch",
                message="File patch has no hunks.",
                line=line_no,
                details={"patch_index": patch_index},
            )
        return None

    i = 0
    while i < len(lines):
        line_no = i + 1
        line = lines[i]

        if line.startswith("diff --git "):
            saw_diff_header = True
            error = finalize_file(line_no)
            if error:
                return _ParseResult(files=[], errors=[error])
            current_file = _FilePatch()
            current_hunk = None
            try:
                header_old, header_new = _parse_diff_header(line, line_no, patch_index)
            except ValueError as exc:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message=str(exc),
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            current_file.header_old = header_old
            current_file.header_new = header_new
            files.append(current_file)
            i += 1
            continue

        if line.startswith("--- "):
            error = finalize_hunk(line_no)
            if error:
                return _ParseResult(files=[], errors=[error])
            if current_file is None:
                if require_git_diff_header:
                    return _ParseResult(
                        files=[],
                        errors=[
                            PatchError(
                                code="invalid_patch",
                                message="Missing diff --git header.",
                                line=line_no,
                                details={"patch_index": patch_index},
                            )
                        ],
                    )
                current_file = _FilePatch()
                files.append(current_file)
            elif current_file.saw_old_header and current_file.saw_new_header:
                error = finalize_file(line_no)
                if error:
                    return _ParseResult(files=[], errors=[error])
                current_file = _FilePatch()
                files.append(current_file)

            try:
                old_path = _parse_file_marker(line, line_no, patch_index)
            except ValueError as exc:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message=str(exc),
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            if old_path is None and current_file.header_old is not None:
                pass
            elif (
                current_file.header_old is not None
                and old_path is not None
                and current_file.header_old != old_path
            ):
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="--- path does not match diff --git header.",
                            line=line_no,
                            file_path=old_path,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            current_file.old_path = old_path
            current_file.saw_old_header = True
            i += 1
            continue

        if line.startswith("+++ "):
            error = finalize_hunk(line_no)
            if error:
                return _ParseResult(files=[], errors=[error])
            if current_file is None or not current_file.saw_old_header:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="Unexpected +++ header before ---.",
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            try:
                new_path = _parse_file_marker(line, line_no, patch_index)
            except ValueError as exc:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message=str(exc),
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            if new_path is None and current_file.header_new is not None:
                pass
            elif (
                current_file.header_new is not None
                and new_path is not None
                and current_file.header_new != new_path
            ):
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="+++ path does not match diff --git header.",
                            line=line_no,
                            file_path=new_path,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            current_file.new_path = new_path
            current_file.saw_new_header = True
            i += 1
            continue

        if line.startswith("@@ "):
            if current_file is None or not (
                current_file.saw_old_header and current_file.saw_new_header
            ):
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="Hunk found before file headers.",
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            error = finalize_hunk(line_no)
            if error:
                return _ParseResult(files=[], errors=[error])
            hunk = _parse_hunk_header(line, line_no)
            if hunk is None:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="Invalid hunk header.",
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            current_file.hunks.append(hunk)
            current_hunk = hunk
            saw_any_hunk = True
            i += 1
            continue

        if current_hunk is not None:
            if current_file is None:
                return _ParseResult(
                    files=[],
                    errors=[
                        PatchError(
                            code="invalid_patch",
                            message="Hunk encountered without file context.",
                            line=line_no,
                            details={"patch_index": patch_index},
                        )
                    ],
                )
            if line.startswith(_NO_NEWLINE_MARKER):
                i += 1
                continue
            if line.startswith(_HUNK_LINE_PREFIXES):
                prefix = line[0]
                if prefix == " ":
                    current_hunk.old_seen += 1
                    current_hunk.new_seen += 1
                elif prefix == "+":
                    current_hunk.additions += 1
                    current_hunk.new_seen += 1
                    current_file.additions += 1
                elif prefix == "-":
                    current_hunk.deletions += 1
                    current_hunk.old_seen += 1
                    current_file.deletions += 1
                i += 1
                continue
            return _ParseResult(
                files=[],
                errors=[
                    PatchError(
                        code="invalid_patch",
                        message="Unexpected line inside hunk.",
                        line=line_no,
                        details={
                            "patch_index": patch_index,
                            "line_text": _truncate_line(line),
                        },
                    )
                ],
            )

        if _is_metadata_line(line):
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        return _ParseResult(
            files=[],
            errors=[
                PatchError(
                    code="invalid_patch",
                    message="Unexpected line outside hunk.",
                    line=line_no,
                    details={
                        "patch_index": patch_index,
                        "line_text": _truncate_line(line),
                    },
                )
            ],
        )

    error = finalize_file(len(lines))
    if error:
        return _ParseResult(files=[], errors=[error])
    if require_git_diff_header and not saw_diff_header:
        return _ParseResult(
            files=[],
            errors=[
                PatchError(
                    code="invalid_patch",
                    message="Patch missing diff --git headers.",
                    details={"patch_index": patch_index},
                )
            ],
        )
    if not saw_any_hunk:
        return _ParseResult(
            files=[],
            errors=[
                PatchError(
                    code="invalid_patch",
                    message="Patch contains no hunks.",
                    details={"patch_index": patch_index},
                )
            ],
        )
    return _ParseResult(files=files, errors=[])


def _parse_diff_header(
    line: str, line_no: int, patch_index: int
) -> tuple[str | None, str | None]:
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    if len(tokens) < 4 or tokens[0] != "diff" or tokens[1] != "--git":
        raise ValueError(
            f"Invalid diff --git header at line {line_no} (patch {patch_index})."
        )
    old_path = _normalize_diff_path(tokens[2], line_no, patch_index)
    new_path = _normalize_diff_path(tokens[3], line_no, patch_index)
    return old_path, new_path


def _parse_file_marker(line: str, line_no: int, patch_index: int) -> str | None:
    raw = line[4:].strip()
    return _normalize_diff_path(raw, line_no, patch_index)


def _parse_hunk_header(line: str, line_no: int) -> _Hunk | None:
    match = _HUNK_RE.match(line)
    if not match:
        LOG.debug("Invalid hunk header at line %s: %s", line_no, line)
        return None
    old_start = int(match.group(1))
    old_count_token = match.group(2)
    old_count = int(old_count_token or 1)
    new_start = int(match.group(3))
    new_count_token = match.group(4)
    new_count = int(new_count_token or 1)
    if old_start < 0 or new_start < 0 or old_count < 0 or new_count < 0:
        return None
    return _Hunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        old_count_explicit=old_count_token is not None,
        new_count_explicit=new_count_token is not None,
    )


def _normalize_diff_path(raw: str, line_no: int, patch_index: int) -> str | None:
    token = _extract_path_token(raw)
    if token == "/dev/null":
        return None
    token = token.replace("\\", "/")
    if token.startswith(("a/", "b/")):
        token = token[2:]
    if token.startswith("./"):
        token = token[2:]
    if not token:
        raise ValueError(
            f"Empty path in patch at line {line_no} (patch {patch_index})."
        )
    if token.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", token):
        raise ValueError(
            f"Absolute path not allowed in patch at line {line_no} (patch {patch_index})."
        )
    parts = [part for part in token.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError(
            f"Unsafe path segment in patch at line {line_no} (patch {patch_index})."
        )
    return "/".join(parts)


def _extract_path_token(raw: str) -> str:
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return raw.strip()
    return tokens[0]


def _is_metadata_line(line: str) -> bool:
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


def _compute_file_stats(
    files: Sequence[_FilePatch], repo_root: Path
) -> tuple[list[PatchFileStats], list[PatchError]]:
    stats: list[PatchFileStats] = []
    errors: list[PatchError] = []
    for file_patch in files:
        path = file_patch.new_path or file_patch.old_path
        if path is None:
            errors.append(
                PatchError(
                    code="invalid_patch",
                    message="File patch missing path.",
                )
            )
            continue
        status = _classify_status(file_patch)
        base_path = file_patch.old_path if file_patch.old_path is not None else None
        deletion_ratio = None
        if base_path is not None:
            abs_path = _resolve_repo_path(repo_root, base_path)
            if abs_path is None:
                errors.append(
                    PatchError(
                        code="invalid_patch",
                        message="File path escapes repository.",
                        file_path=base_path,
                    )
                )
                continue
            if not abs_path.exists():
                errors.append(
                    PatchError(
                        code="invalid_patch",
                        message="File does not exist for modification/delete.",
                        file_path=base_path,
                    )
                )
                continue
            if not abs_path.is_file():
                errors.append(
                    PatchError(
                        code="invalid_patch",
                        message="Target path is not a file.",
                        file_path=base_path,
                    )
                )
                continue
            old_line_count = _count_file_lines(abs_path)
            if old_line_count <= 0:
                deletion_ratio = 1.0 if file_patch.deletions > 0 else 0.0
            else:
                deletion_ratio = file_patch.deletions / old_line_count
        else:
            deletion_ratio = 0.0
        stats.append(
            PatchFileStats(
                path=path,
                status=status,
                additions=file_patch.additions,
                deletions=file_patch.deletions,
                deletion_ratio=deletion_ratio,
            )
        )
    return stats, errors


def _count_file_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return -1


def _resolve_repo_path(repo_root: Path, rel_path: str) -> Path | None:
    abs_path = (repo_root / rel_path).resolve()
    try:
        abs_path.relative_to(repo_root)
    except ValueError:
        return None
    return abs_path


def _classify_status(file_patch: _FilePatch) -> str:
    if file_patch.old_path is None:
        return "added"
    if file_patch.new_path is None:
        return "deleted"
    if file_patch.old_path != file_patch.new_path:
        return "renamed"
    return "modified"


def _check_policy(
    files: Sequence[_FilePatch],
    file_stats: Sequence[PatchFileStats],
    policy: PatchPolicy,
) -> list[PatchError]:
    errors: list[PatchError] = []

    touched_paths = {_canonical_path(file) for file in files}
    if policy.max_files > 0 and len(touched_paths) > policy.max_files:
        errors.append(
            PatchError(
                code="policy_violation",
                message="Patch touches too many files.",
                details={
                    "files_touched": len(touched_paths),
                    "limit": policy.max_files,
                },
            )
        )

    for stat in file_stats:
        if policy.max_deletions_per_file > 0 and (
            stat.deletions > policy.max_deletions_per_file
        ):
            errors.append(
                PatchError(
                    code="policy_violation",
                    message="Deleted lines exceed per-file limit.",
                    file_path=stat.path,
                    details={
                        "deletions": stat.deletions,
                        "limit": policy.max_deletions_per_file,
                    },
                )
            )
        if (
            stat.deletion_ratio is not None
            and policy.max_deletion_ratio >= 0
            and stat.deletions > stat.additions
            and stat.deletion_ratio > policy.max_deletion_ratio
        ):
            errors.append(
                PatchError(
                    code="policy_violation",
                    message="Deleted line ratio exceeds per-file limit.",
                    file_path=stat.path,
                    details={
                        "deletion_ratio": stat.deletion_ratio,
                        "limit": policy.max_deletion_ratio,
                    },
                )
            )
    return errors


def _check_denied_paths(
    files: Sequence[_FilePatch], policy: PatchPolicy
) -> list[PatchError]:
    errors: list[PatchError] = []
    for file_patch in files:
        for path in (file_patch.old_path, file_patch.new_path):
            if path and _is_denied_path(path, policy):
                errors.append(
                    PatchError(
                        code="policy_violation",
                        message="Path is denied by policy.",
                        file_path=path,
                        details={"path": path},
                    )
                )
    return errors


def _canonical_path(file_patch: _FilePatch) -> str:
    return file_patch.new_path or file_patch.old_path or ""


def _is_denied_path(path: str, policy: PatchPolicy) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for prefix in policy.deny_prefixes:
        prefix_norm = prefix.replace("\\", "/")
        if prefix_norm.startswith("./"):
            prefix_norm = prefix_norm[2:]
        if not prefix_norm:
            continue
        if normalized == prefix_norm or normalized.startswith(
            prefix_norm.rstrip("/") + "/"
        ):
            return True
    return any(_match_glob(normalized, pattern) for pattern in policy.deny_globs)


def _match_glob(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    normalized_pattern = pattern.replace("\\", "/").strip()
    if not normalized_pattern:
        return False
    return fnmatch(path, normalized_pattern)


def _run_git_apply(
    repo_root: Path, patch_text: str, *, check_only: bool
) -> PatchError | None:
    cmd = ["git", "-C", str(repo_root), "apply", "--whitespace=nowarn"]
    if check_only:
        cmd.append("--check")
    result = subprocess.run(
        cmd,
        input=patch_text,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        stage = "check" if check_only else "apply"
        details = {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        return PatchError(
            code=f"git_apply_{stage}_failed",
            message=f"git apply {stage} failed.",
            details=details,
        )
    return None


def _join_patches(patches: Sequence[str]) -> str:
    combined = "\n".join(patch.strip("\n") for patch in patches if patch.strip())
    if not combined.endswith("\n"):
        combined += "\n"
    return combined


def _build_stats(file_stats: Sequence[PatchFileStats]) -> PatchStats:
    total_additions = sum(stat.additions for stat in file_stats)
    total_deletions = sum(stat.deletions for stat in file_stats)
    return PatchStats(
        total_additions=total_additions,
        total_deletions=total_deletions,
        files_touched=len(file_stats),
        per_file=list(file_stats),
    )


def _truncate_line(line: str, max_len: int = 200) -> str:
    if len(line) <= max_len:
        return line
    return line[: max_len - 3] + "..."

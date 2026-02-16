from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from megafix.code_agent.patches_engine import PatchPolicy, apply_patches


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def test_apply_patches_ok(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1,2 @@\n"
        " one\n"
        "+two\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=0.3,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is True
    assert result.stats is not None
    assert result.stats.total_additions == 1
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_apply_patches_accepts_index_metadata_line(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "index 5626abf..814f4a4 100644\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1,2 @@\n"
        " one\n"
        "+two\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=0.3,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is True
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_reject_path_traversal(tmp_path: Path) -> None:
    patch = (
        "diff --git a/../secret.txt b/../secret.txt\n"
        "--- a/../secret.txt\n"
        "+++ b/../secret.txt\n"
        "@@ -0,0 +1 @@\n"
        "+oops\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=0.3,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "invalid_patch"


def test_policy_violation_max_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1,2 @@\n"
        " a\n"
        "+x\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -1 +1,2 @@\n"
        " b\n"
        "+y\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=1,
        max_deletions_per_file=200,
        max_deletion_ratio=0.3,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "policy_violation"


def test_apply_patches_fails_when_hunk_context_mismatch(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("one\n", encoding="utf-8")
    patch = (
        "diff --git a/hello.txt b/hello.txt\n"
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-ONE\n"
        "+two\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=1.0,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "git_apply_check_failed"


def test_policy_violation_deletion_ratio(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    lines = "\n".join([f"line{i}" for i in range(1, 11)]) + "\n"
    (tmp_path / "data.txt").write_text(lines, encoding="utf-8")
    patch = (
        "diff --git a/data.txt b/data.txt\n"
        "--- a/data.txt\n"
        "+++ b/data.txt\n"
        "@@ -1,10 +1 @@\n"
        "-line1\n"
        "-line2\n"
        "-line3\n"
        "-line4\n"
        "-line5\n"
        "-line6\n"
        "-line7\n"
        "-line8\n"
        "-line9\n"
        " line10\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=0.5,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is False
    assert result.errors
    assert result.errors[0].code == "policy_violation"


def test_policy_allows_net_additive_replacement(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("line\n", encoding="utf-8")
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,3 @@\n"
        "-line\n"
        "+line\n"
        "+\n"
        "+extra\n"
    )
    policy = PatchPolicy(
        require_git_diff_header=True,
        max_files=50,
        max_deletions_per_file=200,
        max_deletion_ratio=0.3,
        deny_prefixes=(),
        deny_globs=(),
    )
    result = apply_patches([patch], repo_path=tmp_path, policy=policy)
    assert result.ok is True

from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from pathlib import Path

from git import Repo

from agent_core.github_client import RepoInfo

LOG = logging.getLogger(__name__)
_TOKEN_URL_RE = re.compile(r"(x-access-token:)[^@]+@")


def run_git(repo_path: Path, *args: str) -> None:
    cmd = ["git", "-C", str(repo_path), *args]
    LOG.debug("Running git: %s", " ".join(_redact_cmd(cmd)))
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _redact_cmd(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    for arg in cmd:
        if "x-access-token:" in arg and "@github.com" in arg:
            redacted.append(_TOKEN_URL_RE.sub(r"\1***@", arg))
            continue
        redacted.append(arg)
    return redacted


def prepare_repo(
    repo_info: RepoInfo, token: str, *, base_dir: Path, branch: str
) -> Path:
    safe_name = repo_info.full_name.replace("/", "__")
    repo_path = base_dir / safe_name
    repo_path.mkdir(parents=True, exist_ok=True)

    if not (repo_path / ".git").exists():
        has_files = any(repo_path.iterdir())
        if has_files:
            suffix = f"{int(time.time())}-{uuid.uuid4().hex}"
            new_path = base_dir / f"{safe_name}__clone__{suffix}"
            LOG.warning(
                "Repo path exists without .git and is not empty; cloning into %s instead of %s",
                new_path,
                repo_path,
            )
            repo_path = new_path
            repo_path.mkdir(parents=True, exist_ok=True)

    remote_url = f"https://x-access-token:{token}@github.com/{repo_info.full_name}.git"

    if not (repo_path / ".git").exists():
        subprocess.run(
            ["git", "clone", remote_url, str(repo_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        run_git(repo_path, "remote", "set-url", "origin", remote_url)
        run_git(repo_path, "fetch", "origin", "--prune")

    run_git(repo_path, "checkout", repo_info.default_branch)
    run_git(repo_path, "reset", "--hard", f"origin/{repo_info.default_branch}")
    run_git(repo_path, "clean", "-fdx")

    run_git(repo_path, "checkout", "-B", branch)
    return repo_path


def commit_if_needed(repo_path: Path, message: str) -> bool:
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not status.strip():
        return False

    run_git(repo_path, "add", "-A")
    run_git(repo_path, "commit", "-m", message)
    return True


def push_branch(repo_path: Path, branch: str) -> None:
    run_git(repo_path, "push", "-u", "origin", branch, "--force-with-lease")


def get_unique_branch_name(local_repo: Repo, base_name: str) -> str:
    """Return a unique branch name by checking remote branches."""
    remote_branches = [ref.name for ref in local_repo.remote().refs]
    LOG.debug("Found %s remote branches", len(remote_branches))

    if f"origin/{base_name}" not in remote_branches:
        return base_name

    attempt = 1
    while True:
        candidate = f"{base_name}_{attempt}"
        if f"origin/{candidate}" not in remote_branches:
            LOG.debug("Found unique branch name: %s", candidate)
            return candidate
        attempt += 1

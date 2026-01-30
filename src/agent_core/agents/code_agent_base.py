from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class IssueContext:
    number: int
    title: str
    body: str | None


@dataclass(frozen=True)
class CodeAgentResult:
    pr_title: str
    pr_body: str


class CodeAgent(Protocol):
    def run_issue(self, issue: IssueContext, repo_path: Path) -> CodeAgentResult:
        """Apply changes for an issue and return PR title/body."""
        raise NotImplementedError

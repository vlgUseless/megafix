from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IssueContext:
    number: int
    title: str
    body: str | None

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    approve: bool

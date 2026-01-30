import logging
from pathlib import Path

from agent_core.agents.code_agent_base import CodeAgentResult, IssueContext

LOG = logging.getLogger(__name__)


def run_issue(issue: IssueContext, repo_path: Path) -> CodeAgentResult:
    """Apply a deterministic edit for an issue (stub implementation)."""
    LOG.debug("Processing issue #%s: %s", issue.number, issue.title)

    readme_path = repo_path / "README.md"
    content = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    content += (
        f"\n\n## Issue #{issue.number}: {issue.title}\n\nProcessed by megafix agent.\n"
    )
    readme_path.write_text(content, encoding="utf-8")
    LOG.debug("Updated README.md")

    pr_title = f"Fix issue {issue.number}: {issue.title}"
    pr_body = f"Closes #{issue.number}\n\nAutomated changes by megafix agent."

    return CodeAgentResult(pr_title=pr_title, pr_body=pr_body)

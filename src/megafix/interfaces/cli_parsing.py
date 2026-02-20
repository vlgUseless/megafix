import re


def parse_issue_url(issue_url: str) -> tuple[str, str, int]:
    """Extract owner, repo, and issue number from GitHub issue URL."""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", issue_url)
    if not match:
        raise ValueError(f"Invalid issue URL: {issue_url}")
    return match.group(1), match.group(2), int(match.group(3))


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Extract owner, repo, and PR number from GitHub pull request URL."""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError(f"Invalid pull request URL: {pr_url}")
    return match.group(1), match.group(2), int(match.group(3))

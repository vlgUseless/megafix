from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import jwt
import requests

from agent_core.settings import get_settings, load_private_key

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    owner: str
    name: str


def make_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 30, "exp": now + 9 * 60, "iss": app_id}
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def github_request(
    method: str,
    path: str,
    token: str,
    *,
    json_body: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> requests.Response:
    settings = get_settings()
    url = f"{GITHUB_API}{path}"
    user_agent = (
        settings.github_user_agent
        or settings.github_app_name
        or "Issue2PR/0.1 (megaschool-itmo)"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.request(
        method,
        url,
        headers=headers,
        json=json_body,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response


def get_installation_token(installation_id: int) -> str:
    settings = get_settings()
    if not settings.github_app_id:
        raise RuntimeError("Missing GITHUB_APP_ID.")
    private_key_pem = load_private_key(settings)
    app_jwt = make_app_jwt(settings.github_app_id, private_key_pem)
    response = github_request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        token=app_jwt,
    )
    token_value = response.json()["token"]
    return cast(str, token_value)


def get_repo_info(token: str, full_name: str) -> RepoInfo:
    response = github_request("GET", f"/repos/{full_name}", token=token)
    payload = cast(Mapping[str, Any], response.json())
    return RepoInfo(
        full_name=payload["full_name"],
        default_branch=payload["default_branch"],
        owner=payload["owner"]["login"],
        name=payload["name"],
    )


def get_issue(token: str, repo_full_name: str, issue_number: int) -> Mapping[str, Any]:
    response = github_request(
        "GET", f"/repos/{repo_full_name}/issues/{issue_number}", token=token
    )
    return cast(Mapping[str, Any], response.json())


def find_open_pr(token: str, repo_info: RepoInfo, branch: str) -> dict[str, Any] | None:
    head = f"{repo_info.owner}:{branch}"
    response = github_request(
        "GET",
        f"/repos/{repo_info.full_name}/pulls",
        token=token,
        params={"state": "open", "head": head},
    )
    items = cast(list[dict[str, Any]], response.json())
    if not items:
        return None
    return items[0]


def create_or_update_pr(
    token: str,
    repo_info: RepoInfo,
    branch: str,
    *,
    title: str,
    body: str,
) -> str:
    existing = find_open_pr(token, repo_info, branch)
    if existing:
        pr_number = existing["number"]
        github_request(
            "PATCH",
            f"/repos/{repo_info.full_name}/pulls/{pr_number}",
            token=token,
            json_body={"title": title, "body": body},
        )
        return cast(str, existing["html_url"])

    response = github_request(
        "POST",
        f"/repos/{repo_info.full_name}/pulls",
        token=token,
        json_body={
            "title": title,
            "body": body,
            "head": branch,
            "base": repo_info.default_branch,
        },
    )
    return cast(str, response.json()["html_url"])


def create_pr(
    token: str,
    repo_info: RepoInfo,
    branch: str,
    *,
    title: str,
    body: str,
) -> str:
    response = github_request(
        "POST",
        f"/repos/{repo_info.full_name}/pulls",
        token=token,
        json_body={
            "title": title,
            "body": body,
            "head": branch,
            "base": repo_info.default_branch,
        },
    )
    return cast(str, response.json()["html_url"])


def comment_issue(
    token: str, repo_full_name: str, issue_number: int, message: str
) -> None:
    github_request(
        "POST",
        f"/repos/{repo_full_name}/issues/{issue_number}/comments",
        token=token,
        json_body={"body": message},
    )

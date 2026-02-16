from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import requests

from megafix.shared.settings import get_settings


class LLMServiceError(RuntimeError):
    """Raised when the LLM service is misconfigured or returns an error."""


@dataclass(frozen=True)
class LLMServiceConfig:
    base_url: str
    timeout_sec: int
    api_key: str | None = None
    model: str | None = None
    max_tokens: int | None = None


def summarize_review(diff: str, ci_summary: Any, issue: Any) -> str:
    """Generate a markdown review summary based on diff, CI summary, and issue."""
    issue_payload = _issue_payload(issue)
    summary_payload = _to_jsonable(ci_summary)
    system_prompt = (
        "You are a meticulous code reviewer. "
        "Write a compact markdown assessment focused on quality risks and test signals. "
        "Do not repeat PR metadata (title, issue number, file list, CI counters)."
    )
    user_prompt = _render_review_prompt(issue_payload, summary_payload, diff)
    content = _chat_completion(system_prompt, user_prompt)
    if not content.strip():
        raise LLMServiceError("OpenAI response did not include a summary.")
    return content.strip()


def _get_review_config() -> LLMServiceConfig:
    settings = get_settings()
    if not settings.review_llm_service_url:
        raise LLMServiceError(
            "REVIEW_LLM_SERVICE_URL (or LLM_SERVICE_URL) is not configured."
        )
    return LLMServiceConfig(
        base_url=_normalize_base_url(settings.review_llm_service_url),
        timeout_sec=settings.llm_service_timeout_sec,
        api_key=settings.review_llm_service_api_key,
        model=settings.review_llm_service_model,
        max_tokens=settings.review_llm_max_tokens,
    )


def _chat_completion(system_prompt: str, user_prompt: str) -> str:
    config = _get_review_config()
    if not config.api_key:
        raise LLMServiceError(
            "REVIEW_LLM_SERVICE_API_KEY (or LLM_SERVICE_API_KEY / OPENAI_API_KEY) is not set."
        )
    if not config.model:
        raise LLMServiceError(
            "REVIEW_LLM_SERVICE_MODEL (or LLM_SERVICE_MODEL) is not configured."
        )

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
    }
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens
    data = _post_json("/chat/completions", payload)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str):
        raise LLMServiceError("OpenAI response content missing or invalid.")
    return content


def _post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = _get_review_config()
    url = f"{config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=config.timeout_sec,
        )
    except requests.RequestException as exc:
        raise LLMServiceError(f"Failed to reach OpenAI endpoint at {url}.") from exc

    if response.status_code >= 400:
        raise LLMServiceError(f"OpenAI error {response.status_code}: {response.text}")

    try:
        data = response.json()
    except ValueError as exc:
        raise LLMServiceError("OpenAI returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise LLMServiceError("OpenAI response must be a JSON object.")
    return data


def _normalize_base_url(url: str) -> str:
    base = url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _render_review_prompt(issue: Mapping[str, Any], ci_summary: Any, diff: str) -> str:
    issue_json = json.dumps(issue, ensure_ascii=True, indent=2)
    ci_json = json.dumps(ci_summary, ensure_ascii=True, indent=2)
    return (
        "Issue:\n"
        f"{issue_json}\n\n"
        "CI summary:\n"
        f"{ci_json}\n\n"
        "Diff:\n"
        f"{diff}\n\n"
        "Write a compact markdown review with these sections:\n"
        "- Summary (1-3 bullets)\n"
        "- Risks (0-3 bullets)\n"
        "- Tests (0-3 bullets)\n"
        "- Verdict (approve/request changes, one line)\n"
        "Rules:\n"
        "- Avoid repeating metadata (PR title, file counts, run counts).\n"
        "- Mention changed files only when needed to explain risk.\n"
        "- Keep the whole response under ~220 words.\n"
    )


def _issue_payload(issue: Any) -> dict[str, Any]:
    if issue is None:
        return {}
    if isinstance(issue, Mapping):
        return {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "body": issue.get("body"),
        }
    return {
        "number": getattr(issue, "number", None),
        "title": getattr(issue, "title", None),
        "body": getattr(issue, "body", None),
    }


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import requests

from megafix.shared.settings import get_settings

_VALID_FINDING_SEVERITIES = {"low", "medium", "high"}
_VALID_VERDICTS = {"approve", "request_changes"}


class LLMServiceError(RuntimeError):
    """Raised when the LLM service is misconfigured or returns an error."""


@dataclass(frozen=True)
class ReviewFinding:
    title: str
    details: str
    severity: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class StructuredReview:
    summary: tuple[str, ...]
    blocking_findings: tuple[ReviewFinding, ...]
    non_blocking_findings: tuple[ReviewFinding, ...]
    tests: tuple[str, ...]
    verdict: str


@dataclass(frozen=True)
class LLMServiceConfig:
    base_url: str
    timeout_sec: int
    api_key: str | None = None
    model: str | None = None
    max_tokens: int | None = None


def summarize_review(diff: str, ci_summary: Any, issue: Any) -> StructuredReview:
    """Generate a structured review summary based on diff, CI summary, and issue."""
    issue_payload = _issue_payload(issue)
    summary_payload = _to_jsonable(ci_summary)
    system_prompt = (
        "You are a pragmatic senior code reviewer. "
        "Return strict JSON only. "
        "Mark request_changes only for blocking issues introduced by this PR."
    )
    user_prompt = _render_review_prompt(issue_payload, summary_payload, diff)
    content = _chat_completion(system_prompt, user_prompt)
    if not content.strip():
        raise LLMServiceError("OpenAI response did not include a summary.")
    return _parse_structured_review(content)


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
        "Return ONLY valid JSON object with this exact shape:\n"
        "{\n"
        '  "summary": ["string", "..."],\n'
        '  "blocking_findings": [\n'
        "    {\n"
        '      "title": "string",\n'
        '      "details": "string",\n'
        '      "severity": "low|medium|high",\n'
        '      "file": "path/or/null",\n'
        '      "line": 123 or null\n'
        "    }\n"
        "  ],\n"
        '  "non_blocking_findings": [same finding shape],\n'
        '  "tests": ["string", "..."],\n'
        '  "verdict": "approve|request_changes"\n'
        "}\n\n"
        "Hard rules:\n"
        "- request_changes only when there is at least one true blocker.\n"
        "- Nits, pre-existing issues, and missing extra tests are non-blocking.\n"
        "- Base findings only on this diff and CI summary.\n"
        "- Do not include markdown or additional keys.\n"
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


def _parse_structured_review(content: str) -> StructuredReview:
    payload = _load_json_object(content)

    summary = _parse_string_list(payload.get("summary"), field="summary", max_items=4)
    tests = _parse_string_list(payload.get("tests"), field="tests", max_items=4)
    blocking_findings = _parse_findings(
        payload.get("blocking_findings"),
        field="blocking_findings",
        max_items=5,
    )
    non_blocking_findings = _parse_findings(
        payload.get("non_blocking_findings"),
        field="non_blocking_findings",
        max_items=5,
    )

    verdict_raw = str(payload.get("verdict", "")).strip().lower()
    if verdict_raw not in _VALID_VERDICTS:
        raise LLMServiceError(
            "Structured review must set verdict to approve or request_changes."
        )
    normalized_verdict = "request_changes" if blocking_findings else "approve"

    return StructuredReview(
        summary=tuple(summary),
        blocking_findings=tuple(blocking_findings),
        non_blocking_findings=tuple(non_blocking_findings),
        tests=tuple(tests),
        verdict=normalized_verdict,
    )


def _load_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if not text:
        raise LLMServiceError("Structured review response is empty.")

    candidates: list[str] = [text]
    fenced = re.search(
        r"```(?:json)?\s*(\{.*\})\s*```", text, re.IGNORECASE | re.DOTALL
    )
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise LLMServiceError("Structured review response is not valid JSON object.")


def _parse_string_list(value: Any, *, field: str, max_items: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMServiceError(f"Structured review field '{field}' must be a list.")
    items: list[str] = []
    for raw in value[:max_items]:
        text = str(raw).strip()
        if text:
            items.append(text)
    return items


def _parse_findings(value: Any, *, field: str, max_items: int) -> list[ReviewFinding]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMServiceError(f"Structured review field '{field}' must be a list.")

    findings: list[ReviewFinding] = []
    for raw in value[:max_items]:
        if not isinstance(raw, Mapping):
            raise LLMServiceError(f"Finding in '{field}' must be a JSON object.")
        title = str(raw.get("title", "")).strip()
        details = str(raw.get("details", "")).strip()
        if not title or not details:
            raise LLMServiceError(
                f"Finding in '{field}' must include non-empty title and details."
            )
        severity = str(raw.get("severity", "medium")).strip().lower()
        if severity not in _VALID_FINDING_SEVERITIES:
            severity = "medium"

        file_value = raw.get("file")
        file = None
        if file_value is not None:
            file_text = str(file_value).strip()
            file = file_text or None

        line = _parse_optional_positive_int(raw.get("line"))

        findings.append(
            ReviewFinding(
                title=title,
                details=details,
                severity=severity,
                file=file,
                line=line,
            )
        )
    return findings


def _parse_optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import requests

from agent_core.settings import get_settings


class LLMServiceError(RuntimeError):
    """Raised when the LLM service is misconfigured or returns an error."""


@dataclass(frozen=True)
class LLMServiceConfig:
    base_url: str
    timeout_sec: int
    api_key: str | None = None
    model: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str
    content: str | None = None


def generate_file_changes(issue: Any, repo_context: Any) -> list[FileChange]:
    """Request file-level changes for an issue + repo context."""
    issue_payload = _issue_payload(issue)
    repo_payload = _to_jsonable(repo_context)
    system_prompt = (
        "You are a senior software engineer. "
        "You MUST implement the task described in the Issue, not restate it. "
        "Make the smallest possible change set that satisfies the Issue. "
        "Do NOT delete or replace unrelated code, endpoints, tests, or files. "
        "Do NOT remove or rename existing behavior unless the Issue explicitly requires it. "
        "Prefer additive changes and minimal edits to existing code. "
        "Never copy the Issue text into files unless the Issue explicitly asks to embed it. "
        "Return ONLY a JSON object that matches the requested schema. "
        "Do not include markdown fences, explanations, or extra keys."
    )
    user_prompt = _render_changes_prompt(issue_payload, repo_payload)
    content = _chat_completion(system_prompt, user_prompt)
    data = _extract_json(content)
    changes = _parse_changes(data)
    if not changes:
        raise LLMServiceError("OpenAI response did not include any file changes.")
    return changes


def summarize_review(diff: str, ci_summary: Any, issue: Any) -> str:
    """Generate a markdown review summary based on diff, CI summary, and issue."""
    issue_payload = _issue_payload(issue)
    summary_payload = _to_jsonable(ci_summary)
    system_prompt = (
        "You are a meticulous code reviewer. "
        "Write a concise markdown review summary with risks, tests, and verdict."
    )
    user_prompt = _render_review_prompt(issue_payload, summary_payload, diff)
    content = _chat_completion(system_prompt, user_prompt)
    if not content.strip():
        raise LLMServiceError("OpenAI response did not include a summary.")
    return content.strip()


def _get_config() -> LLMServiceConfig:
    settings = get_settings()
    if not settings.llm_service_url:
        raise LLMServiceError("LLM_SERVICE_URL is not configured.")
    return LLMServiceConfig(
        base_url=_normalize_base_url(settings.llm_service_url),
        timeout_sec=settings.llm_service_timeout_sec,
        api_key=settings.llm_service_api_key,
        model=settings.llm_service_model,
        max_tokens=settings.llm_max_tokens,
    )


def _chat_completion(system_prompt: str, user_prompt: str) -> str:
    config = _get_config()
    if not config.api_key:
        raise LLMServiceError("LLM_SERVICE_API_KEY (or OPENAI_API_KEY) is not set.")
    if not config.model:
        raise LLMServiceError("LLM_SERVICE_MODEL is not configured.")

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
    config = _get_config()
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


def _render_changes_prompt(issue: Mapping[str, Any], repo_context: Any) -> str:
    context_json = json.dumps(repo_context, ensure_ascii=True, indent=2)
    issue_json = json.dumps(issue, ensure_ascii=True, indent=2)
    return (
        "Issue:\n"
        f"{issue_json}\n\n"
        "Repo context (tree and selected files):\n"
        f"{context_json}\n\n"
        "Return a JSON object with this schema:\n"
        "{\n"
        '  "files": [\n'
        "    {\n"
        '      "path": "relative/path/to/file.ext",\n'
        '      "action": "add|modify|delete",\n'
        '      "content": "FULL FILE CONTENT AFTER CHANGE (omit for delete)"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- Only use paths that exist in repo_tree or new paths for add.\n"
        "- For modify, include full file content after your change.\n"
        "- For delete, omit content.\n"
        "- The changes must satisfy the Issue requirements (e.g., if asked for a list, output a real list).\n"
        "- Do NOT just copy the Issue text into files unless explicitly required.\n"
        "- No extra keys, no explanations.\n"
    )


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
        "Write a markdown review summary with:\n"
        "- Summary\n"
        "- Risks\n"
        "- Tests\n"
        "- Verdict (approve/request changes)\n"
    )


def _extract_json(text: str) -> object:
    candidates = [text.strip()]
    if "```" in text:
        candidates.extend(_extract_fenced_blocks(text))

    for candidate in candidates:
        for snippet in _iter_json_candidates(candidate):
            try:
                data = json.loads(snippet)
            except json.JSONDecodeError:
                continue
            if isinstance(data, (dict, list)):
                return data
    raise LLMServiceError("OpenAI response did not include valid JSON.")


def _iter_json_candidates(text: str) -> Iterable[str]:
    stripped = text.strip()
    if stripped:
        yield stripped
    obj_start = stripped.find("{")
    obj_end = stripped.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        yield stripped[obj_start : obj_end + 1]
    arr_start = stripped.find("[")
    arr_end = stripped.rfind("]")
    if arr_start != -1 and arr_end > arr_start:
        yield stripped[arr_start : arr_end + 1]


def _extract_fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_block:
                blocks.append("\n".join(current))
                current = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current.append(line)
    return blocks


def _parse_changes(data: object) -> list[FileChange]:
    items: object
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        candidate = data.get("files") or data.get("changes")
        if candidate is None:
            raise LLMServiceError("JSON response missing 'files' list.")
        items = candidate
    else:
        raise LLMServiceError("JSON response must be an object or list.")

    if not isinstance(items, list):
        raise LLMServiceError("'files' must be a list.")

    changes: list[FileChange] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise LLMServiceError("Each file change must be an object.")
        path = str(entry.get("path") or "").strip()
        action = str(entry.get("action") or "").strip().lower()
        content = entry.get("content")
        if not path:
            raise LLMServiceError("File change missing path.")
        if action not in {"add", "modify", "delete"}:
            raise LLMServiceError(f"Invalid action '{action}' for {path}.")
        if action in {"add", "modify"} and not isinstance(content, str):
            raise LLMServiceError(f"Missing content for {action} {path}.")
        if action == "delete":
            content = None
        normalized = path.replace("\\", "/").lstrip("./")
        changes.append(FileChange(path=normalized, action=action, content=content))

    return changes


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

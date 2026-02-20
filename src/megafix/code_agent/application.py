from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from megafix.code_agent.orchestration import run_patch_agent
from megafix.shared.schemas import IssueContext
from megafix.shared.settings import Settings


@dataclass(frozen=True)
class CodeAgentResultV2:
    pr_title: str
    pr_body: str
    final_message: str | None
    checks_ok: bool
    iterations: int


def run_issue_graph(
    issue: IssueContext,
    repo_path: Path,
    settings: Settings,
    progress_cb: Callable[[str], None] | None = None,
) -> CodeAgentResultV2:
    _ensure_llm_settings(settings)
    if progress_cb:
        progress_cb("Starting LangGraph patch loop.")

    llm = _build_llm(settings)
    max_iterations = _resolve_agent_max_iterations(settings)
    if _supports_max_iterations_kwarg(run_patch_agent):
        state = run_patch_agent(
            llm,
            issue,
            repo_path=repo_path,
            max_iterations=max_iterations,
        )
    else:
        state = run_patch_agent(
            llm,
            issue,
            repo_path=repo_path,
        )

    final_message = _extract_final_message(state.get("messages", []))
    checks_ok = bool(state.get("checks_ok"))
    iterations = int(state.get("iterations", 0))
    check_results = state.get("check_results", [])
    changed_files = _list_changed_files(repo_path)
    if progress_cb:
        status = "passed" if checks_ok else "failed"
        progress_cb(f"Checks {status} after {iterations} iteration(s).")

    title = f"Fix issue {issue.number}: {issue.title}"
    body = _build_pr_body(
        issue=issue,
        final_message=final_message,
        checks_ok=checks_ok,
        check_results=check_results,
        changed_files=changed_files,
    )
    return CodeAgentResultV2(
        pr_title=title,
        pr_body=body,
        final_message=final_message,
        checks_ok=checks_ok,
        iterations=iterations,
    )


def _ensure_llm_settings(settings: Settings) -> None:
    if not settings.llm_service_url:
        raise RuntimeError("LLM_SERVICE_URL is not configured. LLM agent is required.")
    if not settings.llm_service_api_key:
        raise RuntimeError("LLM_SERVICE_API_KEY (or OPENAI_API_KEY) is not set.")
    if not settings.llm_service_model:
        raise RuntimeError("LLM_SERVICE_MODEL is not configured.")


def _resolve_agent_max_iterations(settings: Settings, default: int = 3) -> int:
    raw = getattr(settings, "agent_max_iterations", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, value)


def _supports_max_iterations_kwarg(func: Callable[..., object]) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return True
    parameters = signature.parameters.values()
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters):
        return True
    return "max_iterations" in signature.parameters


def _build_llm(settings: Settings) -> Any:
    try:
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "langchain-openai is required to run the LangGraph agent."
        ) from exc

    base_url = _normalize_base_url(settings.llm_service_url or "")
    model = settings.llm_service_model or ""
    api_key = settings.llm_service_api_key or ""
    llm_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": SecretStr(api_key),
        "base_url": base_url,
        "temperature": 0,
        "timeout": settings.llm_service_timeout_sec,
    }
    if settings.llm_max_tokens is not None:
        # Newer langchain-openai/openai stacks use completion-token naming.
        llm_kwargs["max_completion_tokens"] = settings.llm_max_tokens
    chat_cls: Any = ChatOpenAI
    return chat_cls(**llm_kwargs)


def _normalize_base_url(url: str) -> str:
    base = url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _extract_final_message(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if hasattr(message, "tool_call_id"):
            continue
        class_name = type(message).__name__
        if class_name in {"SystemMessage", "HumanMessage", "ToolMessage"}:
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        if _looks_like_internal_agent_log_message(text):
            continue
        return text
    return None


def _build_pr_body(
    *,
    issue: IssueContext,
    final_message: str | None,
    checks_ok: bool,
    check_results: object,
    changed_files: list[str],
) -> str:
    lines = [f"Closes #{issue.number}", "", "## What Changed"]
    summary_lines = _extract_summary_lines(final_message)
    if summary_lines:
        for item in summary_lines:
            lines.append(f"- {item}")
    elif changed_files:
        lines.append(f"- Updated {len(changed_files)} file(s) to resolve the issue.")
    else:
        lines.append("- Applied automated changes to resolve the issue.")

    if changed_files:
        lines.append("")
        lines.append("## Touched Files")
        display_limit = 8
        for file_path in changed_files[:display_limit]:
            lines.append(f"- `{file_path}`")
        remaining = len(changed_files) - display_limit
        if remaining > 0:
            lines.append(f"- ... and {remaining} more file(s)")

    lines.append("")
    lines.append("## Validation")
    rendered_checks = _render_check_results(check_results)
    if rendered_checks:
        lines.extend(rendered_checks)
    else:
        if checks_ok:
            lines.append("- No explicit check commands were executed.")
        else:
            lines.append("- Checks failed (no command details available).")

    lines.append("")
    lines.append("## Notes")
    lines.append("- This PR was generated automatically by megafix agent.")
    lines.append("- Detailed quality assessment is posted by the reviewer agent.")
    return "\n".join(lines).strip() + "\n"


def _extract_summary_lines(
    final_message: str | None, *, max_items: int = 5
) -> list[str]:
    if not final_message:
        return []
    lines: list[str] = []
    for raw in final_message.splitlines():
        text = raw.strip()
        if not text:
            continue
        if _looks_like_internal_agent_log_message(text):
            continue
        if text.startswith(("#", "---", "```")):
            continue
        text = text.lstrip("-* ").strip()
        if text and text[0].isdigit():
            parts = text.split(".", 1)
            if len(parts) == 2 and parts[0].isdigit():
                text = parts[1].strip()
        if not text:
            continue
        if len(text) > 180:
            text = text[:177].rstrip() + "..."
        if text not in lines:
            lines.append(text)
        if len(lines) >= max_items:
            break
    return lines


def _render_check_results(check_results: object) -> list[str]:
    if not isinstance(check_results, list) or not check_results:
        return []
    parsed: list[tuple[int, str, int]] = []
    for index, item in enumerate(check_results):
        command = getattr(item, "command", None)
        exit_code = getattr(item, "exit_code", None)
        if isinstance(item, dict):
            command = item.get("command", command)
            exit_code = item.get("exit_code", exit_code)
        if not isinstance(command, str):
            continue
        if not isinstance(exit_code, int):
            continue
        parsed.append((index, command, exit_code))

    if not parsed:
        return []

    latest_by_command: dict[str, tuple[int, int]] = {}
    for index, command, exit_code in parsed:
        latest_by_command[command] = (index, exit_code)

    lines: list[str] = []
    for command, (_, exit_code) in sorted(
        latest_by_command.items(), key=lambda item: item[1][0]
    ):
        status = "passed" if exit_code == 0 else f"failed (exit {exit_code})"
        lines.append(f"- `{command}`: {status}")
    return lines


def _looks_like_internal_agent_log_message(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith("run_checks result:") or lowered.startswith(
        "run_checks summary:"
    )


def _list_changed_files(repo_path: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        path_part = line[3:] if len(line) > 3 else line
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path = path_part.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files

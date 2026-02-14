from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.agents.code_agent_base import IssueContext
from agent_core.orchestrator.langgraph_cycle import run_patch_agent
from agent_core.settings import Settings


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
    state = run_patch_agent(llm, issue, repo_path=repo_path)

    final_message = _extract_final_message(state.get("messages", []))
    checks_ok = bool(state.get("checks_ok"))
    iterations = int(state.get("iterations", 0))
    if progress_cb:
        status = "passed" if checks_ok else "failed"
        progress_cb(f"Checks {status} after {iterations} iteration(s).")

    title = f"Fix issue {issue.number}: {issue.title}"
    body = f"Closes #{issue.number}\n\nAutomated changes by megafix agent."
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


def _build_llm(settings: Settings) -> Any:
    try:
        from langchain_openai import ChatOpenAI
    except Exception as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError(
            "langchain-openai is required to run the LangGraph agent."
        ) from exc

    base_url = _normalize_base_url(settings.llm_service_url or "")
    return ChatOpenAI(
        model=settings.llm_service_model,
        api_key=settings.llm_service_api_key,
        base_url=base_url,
        temperature=0,
        timeout=settings.llm_service_timeout_sec,
        max_tokens=settings.llm_max_tokens,
    )


def _normalize_base_url(url: str) -> str:
    base = url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _extract_final_message(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if hasattr(message, "tool_call_id"):
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None

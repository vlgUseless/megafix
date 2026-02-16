from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from megafix.code_agent.tool_registry import get_tool_definitions, get_tool_handler
from megafix.shared.schemas import IssueContext
from megafix.shared.settings import get_settings

LOG = logging.getLogger(__name__)

HUMAN_MESSAGE_CLS: Any = None
SYSTEM_MESSAGE_CLS: Any = None
TOOL_MESSAGE_CLS: Any = None
STATE_GRAPH_CLS: Any = None
END_SENTINEL: Any = None

try:  # Optional runtime dependency.
    from langchain_core.messages import HumanMessage as _HumanMessage
    from langchain_core.messages import SystemMessage as _SystemMessage
    from langchain_core.messages import ToolMessage as _ToolMessage
    from langgraph.graph import END as _END
    from langgraph.graph import StateGraph as _StateGraph

    HUMAN_MESSAGE_CLS = _HumanMessage
    SYSTEM_MESSAGE_CLS = _SystemMessage
    TOOL_MESSAGE_CLS = _ToolMessage
    STATE_GRAPH_CLS = _StateGraph
    END_SENTINEL = _END
except Exception:  # pragma: no cover - handled at runtime.
    pass


class LangGraphUnavailable(RuntimeError):
    """Raised when LangGraph/LangChain is not installed."""


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


class AgentState(TypedDict):
    messages: list[Any]
    repo_path: Path
    iterations: int
    max_iterations: int
    patch_attempts: int
    max_patch_attempts: int
    tool_turns: int
    max_tool_turns: int
    max_tool_calls_per_turn: int
    apply_done: bool
    checks_done: bool
    checks_ok: bool
    check_results: list[CheckResult]
    force_final: bool
    last_error_code: str | None
    same_error_count: int
    max_same_error: int
    tool_overview_logged: bool


class ToolCall(TypedDict):
    name: str
    args: dict[str, object]
    id: str


def build_patch_agent_graph(llm: Any, *, repo_path: Path, max_iterations: int = 3):
    if STATE_GRAPH_CLS is None or END_SENTINEL is None:
        raise LangGraphUnavailable(
            "LangGraph/LangChain not installed. Add langgraph/langchain to dependencies."
        )

    tool_defs = get_tool_definitions()
    llm_with_tools = llm.bind_tools(tool_defs)

    def assistant(state: AgentState) -> dict[str, object]:
        trimmed_messages = _trim_messages(state["messages"])
        first_tool_overview = not state.get("tool_overview_logged", False)
        _log_tool_payload(tool_defs, first_tool_overview=first_tool_overview)
        _log_messages(trimmed_messages)
        try:
            response = llm_with_tools.invoke(trimmed_messages)
        except Exception as exc:  # pragma: no cover - runtime diagnostics
            _log_llm_error(exc)
            raise
        _log_response(response)
        return {
            "messages": _trim_messages(trimmed_messages + [response]),
            "tool_overview_logged": True,
        }

    def tool_exec(state: AgentState) -> dict[str, object]:
        last = state["messages"][-1]
        tool_calls = _extract_tool_calls(last)
        if not tool_calls:
            return {}

        messages: list[Any] = []
        pending_humans: list[Any] = []
        tool_turns = state.get("tool_turns", 0)
        max_tool_turns = state.get("max_tool_turns", 0)
        max_calls_per_turn = state.get("max_tool_calls_per_turn", 0)
        if max_tool_turns and tool_turns >= max_tool_turns:
            for call in tool_calls:
                messages.append(
                    _tool_message(
                        {
                            "errors": [
                                {
                                    "code": "tool_limit_reached",
                                    "message": (
                                        "Tool call skipped due to tool turn limit."
                                    ),
                                }
                            ]
                        },
                        call["id"],
                        call["name"],
                    )
                )
            if HUMAN_MESSAGE_CLS is not None:
                messages.append(
                    HUMAN_MESSAGE_CLS(
                        content=(
                            "Tool limit reached; stop tool calls and provide a final "
                            "summary with current status."
                        )
                    )
                )
            return {
                "messages": state["messages"] + messages,
                "force_final": True,
            }

        if max_calls_per_turn and len(tool_calls) > max_calls_per_turn:
            if HUMAN_MESSAGE_CLS is not None:
                pending_humans.append(
                    HUMAN_MESSAGE_CLS(
                        content=(
                            "Too many tool calls in one turn. "
                            f"Only the first {max_calls_per_turn} will be executed."
                        )
                    )
                )
            tool_calls = tool_calls[:max_calls_per_turn]

        tool_turns += 1
        apply_done = state.get("apply_done", False)
        checks_done = state.get("checks_done", False)
        force_final = state.get("force_final", False)
        last_error_code = state.get("last_error_code")
        same_error_count = state.get("same_error_count", 0)
        max_same_error = state.get("max_same_error", 0)
        patch_attempts = state.get("patch_attempts", 0)
        max_patch_attempts = state.get("max_patch_attempts", 0)
        for call in tool_calls:
            name = call["name"]
            args = call["args"]
            call_id = call["id"]
            handler = get_tool_handler(name)
            try:
                result = handler(args, repo_path=repo_path)
            except Exception as exc:  # pragma: no cover - runtime safety
                result = {"error": str(exc)}
            if name in {"repo_apply_edits", "repo_apply_patches"} and isinstance(
                result, dict
            ):
                apply_done = bool(result.get("applied"))
                if apply_done:
                    checks_done = False
                    force_final = False
            error_code: str | None = None
            if name in {
                "repo_propose_edits",
                "repo_apply_edits",
                "repo_propose_patches",
                "repo_apply_patches",
            } and isinstance(result, dict):
                error_code = _extract_first_error_code(result)
            if name in {"repo_propose_edits", "repo_propose_patches"} and isinstance(
                result, dict
            ):
                accepted = result.get("accepted")
                _log_repo_propose_errors(name, result)
                if accepted is False:
                    # Context mismatch is often recoverable after a fresh read.
                    # Do not consume the patch budget for this class of errors.
                    if error_code in {"context_conflict", "anchor_not_found"}:
                        LOG.info(
                            "Patch proposal rejected with %s; patch attempt budget unchanged.",
                            error_code,
                        )
                    else:
                        patch_attempts += 1
                        if max_patch_attempts and patch_attempts >= max_patch_attempts:
                            force_final = True
                            LOG.warning(
                                "Patch attempts limit reached: %s/%s",
                                patch_attempts,
                                max_patch_attempts,
                            )
                            if HUMAN_MESSAGE_CLS is not None:
                                pending_humans.append(
                                    HUMAN_MESSAGE_CLS(
                                        content=(
                                            "Patch could not be accepted after multiple "
                                            "attempts. Stop tool calls and provide a final "
                                            "summary with current status."
                                        )
                                    )
                                )
            if name in {
                "repo_propose_edits",
                "repo_apply_edits",
                "repo_propose_patches",
                "repo_apply_patches",
            } and isinstance(result, dict):
                if error_code:
                    if (
                        error_code == "context_conflict"
                        and name in {"repo_propose_edits", "repo_apply_edits"}
                        and HUMAN_MESSAGE_CLS is not None
                    ):
                        pending_humans.append(
                            HUMAN_MESSAGE_CLS(
                                content=_build_context_conflict_hint(result)
                            )
                        )
                    if error_code == last_error_code:
                        same_error_count += 1
                    else:
                        last_error_code = error_code
                        same_error_count = 1
                    if (
                        max_same_error
                        and error_code
                        in {"git_apply_check_failed", "git_apply_apply_failed"}
                        and same_error_count >= max_same_error
                    ):
                        force_final = True
                        if HUMAN_MESSAGE_CLS is not None:
                            pending_humans.append(
                                HUMAN_MESSAGE_CLS(
                                    content=(
                                        "Repeated git apply failures detected. "
                                        "Stop tool calls and provide a final summary, "
                                        "including the last error details."
                                    )
                                )
                            )
                else:
                    last_error_code = None
                    same_error_count = 0
            messages.append(_tool_message(result, call_id, name))

        if pending_humans:
            messages.extend(pending_humans)
        return {
            "messages": _trim_messages(state["messages"] + messages),
            "apply_done": apply_done,
            "checks_done": checks_done,
            "force_final": force_final,
            "tool_turns": tool_turns,
            "last_error_code": last_error_code,
            "same_error_count": same_error_count,
            "patch_attempts": patch_attempts,
        }

    def run_checks(state: AgentState) -> dict[str, object]:
        handler = get_tool_handler("run_checks")
        raw_result = handler({}, repo_path=repo_path)
        results, checks_ok = _parse_check_results(raw_result)
        messages = list(state["messages"])
        if SYSTEM_MESSAGE_CLS is not None:
            messages.append(
                SYSTEM_MESSAGE_CLS(
                    content=(
                        "run_checks result: "
                        f"{_summarize_check_results_for_llm(results, checks_ok=checks_ok)}"
                    )
                )
            )
        if not checks_ok:
            messages.append(HUMAN_MESSAGE_CLS(content=_format_check_failure(results)))
        next_iterations = state["iterations"] + (0 if checks_ok else 1)
        # Successful checks do not force-stop the loop: the model may still have
        # pending tool calls (e.g. missed docs update in the same issue).
        force_final = next_iterations >= state["max_iterations"]
        return {
            "messages": _trim_messages(messages),
            "check_results": state.get("check_results", []) + results,
            "checks_done": True,
            "checks_ok": checks_ok,
            "iterations": next_iterations,
            "apply_done": False,
            "force_final": force_final,
        }

    def has_tool_calls(state: AgentState) -> bool:
        # Global stop switch used by guardrails (tool limit, repeated hard failures).
        if state.get("force_final"):
            return False
        last = state["messages"][-1]
        return bool(_extract_tool_calls(last))

    def should_run_checks(state: AgentState) -> bool:
        return bool(state.get("apply_done")) and not state.get("checks_done")

    graph = STATE_GRAPH_CLS(AgentState)
    graph.add_node("assistant", assistant)
    graph.add_node("tools", tool_exec)
    graph.add_node("run_checks", run_checks)

    graph.add_conditional_edges(
        "assistant", has_tool_calls, {True: "tools", False: END_SENTINEL}
    )
    graph.add_conditional_edges(
        "tools",
        should_run_checks,
        {True: "run_checks", False: "assistant"},
    )
    graph.add_edge("run_checks", "assistant")
    graph.set_entry_point("assistant")
    return graph.compile()


def run_patch_agent(
    llm: Any,
    issue: IssueContext,
    *,
    repo_path: Path,
    max_iterations: int = 3,
) -> AgentState:
    graph = build_patch_agent_graph(
        llm, repo_path=repo_path, max_iterations=max_iterations
    )
    system_prompt = _system_prompt()
    user_prompt = _issue_prompt(issue)
    settings = get_settings()
    max_patch_attempts = int(getattr(settings, "agent_max_patch_attempts", 4))
    max_tool_calls_per_turn = int(getattr(settings, "tool_max_calls_per_turn", 6))
    state: AgentState = {
        "messages": [
            SYSTEM_MESSAGE_CLS(content=system_prompt),
            HUMAN_MESSAGE_CLS(content=user_prompt),
        ],
        "repo_path": repo_path,
        "iterations": 0,
        "max_iterations": max_iterations,
        "patch_attempts": 0,
        "max_patch_attempts": max(0, max_patch_attempts),
        "tool_turns": 0,
        "max_tool_turns": max(6, max_iterations * 4),
        "max_tool_calls_per_turn": max(1, max_tool_calls_per_turn),
        "apply_done": False,
        "checks_done": False,
        "checks_ok": False,
        "check_results": [],
        "force_final": False,
        "last_error_code": None,
        "same_error_count": 0,
        "max_same_error": 3,
        "tool_overview_logged": False,
    }
    return cast(AgentState, graph.invoke(state))


def _system_prompt() -> str:
    return (
        "You are a patch-first code agent. "
        "Follow the strict tool order: "
        "1) repo_list_files to see the tree. "
        "2) repo_grep/repo_read_file to collect context. "
        "3) repo_propose_edits to validate structured edits. "
        "4) repo_apply_edits to apply those edits only if accepted. "
        "Prefer structured edits over raw unified diff. "
        "Use repo_propose_patches/repo_apply_patches only as fallback if edit tools "
        "cannot express the required change. "
        "For replace_range/delete_range, start_line/end_line are 1-based and "
        "inclusive; expected_old_text must match exactly. "
        "For insert_after, line is 1-based and expected_old_text must match the "
        "anchor line exactly. "
        "For create_file, set start_line/end_line/line to null and "
        'expected_old_text to "". '
        "After apply, checks will run automatically. "
        "If checks fail, you will receive logs and must propose a new patch. "
        "After checks pass (or retries are exhausted), respond with a brief final "
        "summary and no tool calls."
    )


def _issue_prompt(issue: IssueContext) -> str:
    body = issue.body or ""
    return f"Issue #{issue.number}: {issue.title}\n\n{body}".strip()


def _extract_tool_calls(message: Any) -> list[ToolCall]:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        return [_normalize_tool_call(call) for call in tool_calls]
    additional = getattr(message, "additional_kwargs", {})
    raw_calls = additional.get("tool_calls") if isinstance(additional, dict) else None
    if isinstance(raw_calls, list):
        return [_normalize_openai_tool_call(call) for call in raw_calls]
    return []


def _normalize_tool_call(call: Any) -> ToolCall:
    name = getattr(call, "name", None)
    if name is None and isinstance(call, dict):
        name = call.get("name")
    args = getattr(call, "args", None)
    if args is None and isinstance(call, dict):
        args = call.get("args")
    call_id = getattr(call, "id", None)
    if call_id is None and isinstance(call, dict):
        call_id = call.get("id")
    return {
        "name": str(name or "unknown"),
        "args": _normalize_tool_args(args),
        "id": str(call_id or "tool_call"),
    }


def _normalize_openai_tool_call(call: Any) -> ToolCall:
    if not isinstance(call, dict):
        return {"name": "unknown", "args": {}, "id": "tool_call"}
    function = call.get("function") or {}
    args = function.get("arguments", {})
    return {
        "name": str(function.get("name") or "unknown"),
        "args": _normalize_tool_args(args),
        "id": str(call.get("id") or "tool_call"),
    }


def _normalize_tool_args(args: Any) -> dict[str, object]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            data = json.loads(args)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _tool_message(payload: object, call_id: str, name: str):
    if TOOL_MESSAGE_CLS is None:
        raise LangGraphUnavailable("LangChain is required for ToolMessage.")
    content = json.dumps(payload, ensure_ascii=False)
    return TOOL_MESSAGE_CLS(content=content, tool_call_id=call_id, name=name)


def _parse_check_results(payload: object) -> tuple[list[CheckResult], bool]:
    if not isinstance(payload, dict):
        return [], False
    raw_ok = payload.get("ok")
    explicit_ok = raw_ok if isinstance(raw_ok, bool) else None
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return [], False
    results: list[CheckResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        exit_code = item.get("exit_code")
        stdout = item.get("stdout")
        stderr = item.get("stderr")
        if not isinstance(command, str):
            continue
        if not isinstance(exit_code, int):
            continue
        if not isinstance(stdout, str):
            stdout = str(stdout or "")
        if not isinstance(stderr, str):
            stderr = str(stderr or "")
        results.append(
            CheckResult(
                command=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        )
    computed_ok = all(item.exit_code == 0 for item in results)
    if explicit_ok is None:
        return results, computed_ok
    if not results:
        return results, explicit_ok
    return results, explicit_ok and computed_ok


def _format_check_failure(
    results: list[CheckResult], *, max_log_chars: int = 1500
) -> str:
    lines = ["Checks failed. Logs:"]
    failed = [item for item in results if item.exit_code != 0]
    if not failed:
        return "Checks failed."
    for res in failed:
        status = "ok" if res.exit_code == 0 else f"exit={res.exit_code}"
        lines.append(f"\n$ {res.command} [{status}]")
        if res.stdout:
            lines.append(f"stdout:\n{_truncate_text_tail(res.stdout, max_log_chars)}")
        if res.stderr:
            lines.append(f"stderr:\n{_truncate_text_tail(res.stderr, max_log_chars)}")
    return "\n".join(lines)


def _summarize_check_results_for_llm(
    results: list[CheckResult], *, checks_ok: bool, max_log_chars: int = 800
) -> str:
    payload: dict[str, object] = {"ok": checks_ok, "results": []}
    summarized: list[dict[str, object]] = []
    for item in results:
        entry: dict[str, object] = {
            "command": item.command,
            "exit_code": item.exit_code,
        }
        if item.exit_code != 0:
            if item.stdout:
                entry["stdout"] = _truncate_text_tail(item.stdout, max_log_chars)
            if item.stderr:
                entry["stderr"] = _truncate_text_tail(item.stderr, max_log_chars)
        elif item.stdout:
            # Keep success payload minimal to reduce token usage.
            entry["note"] = _truncate_text_tail(item.stdout, 200)
        summarized.append(entry)
    payload["results"] = summarized
    return json.dumps(payload, ensure_ascii=False)


def _extract_first_error_code(payload: dict[str, object]) -> str | None:
    first = _extract_first_error(payload)
    if not isinstance(first, dict):
        return None
    code = first.get("code")
    if isinstance(code, str) and code:
        return code
    return None


def _extract_first_error(payload: dict[str, object]) -> dict[str, object] | None:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if not isinstance(first, dict):
        return None
    return first


def _build_context_conflict_hint(payload: dict[str, object]) -> str:
    first = _extract_first_error(payload) or {}
    details = first.get("details")
    file_path = first.get("file_path") or first.get("path") or "unknown file"
    op = None
    actual_old_text: str | None = None
    if isinstance(details, dict):
        raw_op = details.get("op")
        if isinstance(raw_op, str) and raw_op:
            op = raw_op
        raw_actual = details.get("actual_old_text")
        if isinstance(raw_actual, str) and raw_actual:
            actual_old_text = raw_actual
    op_text = op or "edit"
    hint = (
        f"Detected context_conflict for {file_path} ({op_text}). "
        "Retry with fresh context: read the relevant file/range again via repo_read_file, "
        "then re-run repo_propose_edits. "
        "Set expected_old_text to details.actual_old_text from the last tool error "
        "exactly (character-for-character, including whitespace and newlines). "
        "Do not paraphrase, trim, or reformat this text."
    )
    if not actual_old_text:
        return hint
    if len(actual_old_text) > 3000:
        return (
            hint
            + " actual_old_text is too long to inline; copy it directly from the last "
            "tool error payload."
        )
    return (
        hint
        + "\n\nReuse this exact value:\n<actual_old_text>\n"
        + actual_old_text
        + "\n</actual_old_text>"
    )


def _log_tool_payload(
    tool_defs: list[dict[str, object]], *, first_tool_overview: bool
) -> None:
    names: list[str] = []
    for tool in tool_defs:
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str):
                names.append(name)
    if first_tool_overview:
        LOG.info(
            "LLM tools: count=%s names=%s tool_choice=auto",
            len(tool_defs),
            names,
        )
    else:
        LOG.debug(
            "LLM tools: count=%s names=%s tool_choice=auto",
            len(tool_defs),
            names,
        )
    summary: list[dict[str, object]] = []
    for tool in tool_defs:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        params = function.get("parameters")
        summary.append(
            {
                "name": name,
                "parameters": _summarize_schema(params),
            }
        )
    LOG.debug("LLM tool schemas: %s", json.dumps(summary, ensure_ascii=False))
    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug(
            "LLM tool definitions JSON: %s", json.dumps(tool_defs, ensure_ascii=False)
        )


def _log_messages(messages: list[Any]) -> None:
    summary: list[dict[str, object]] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        content_type = type(content).__name__
        content_len = len(content) if isinstance(content, str) else None
        additional = getattr(msg, "additional_kwargs", None)
        extra_keys: list[str] | None = None
        if isinstance(additional, dict):
            extra_keys = sorted(additional.keys())
        summary.append(
            {
                "type": type(msg).__name__,
                "content_type": content_type,
                "content_len": content_len,
                "extra_keys": extra_keys,
            }
        )
    LOG.debug("LLM messages summary: %s", summary)


def _log_response(response: Any) -> None:
    tool_calls = _extract_tool_calls(response)
    names = [call.get("name", "unknown") for call in tool_calls]
    LOG.info(
        "LLM response: has_tool_calls=%s tool_calls=%s",
        bool(tool_calls),
        names,
    )


def _log_repo_propose_errors(tool_name: str, result: dict[str, object]) -> None:
    errors = result.get("errors")
    if not isinstance(errors, list) or not errors:
        return
    first = errors[0] if errors else None
    if isinstance(first, dict):
        code = first.get("code")
        message = first.get("message")
        file_path = first.get("file_path") or first.get("path")
        details = first.get("details")
        expected_short: str | None = None
        actual_short: str | None = None
        if isinstance(details, dict):
            expected_short = _shorten_log_text(details.get("expected_old_text"))
            actual_short = _shorten_log_text(details.get("actual_old_text"))
        if expected_short is not None or actual_short is not None:
            LOG.warning(
                "%s errors: count=%s first_code=%s first_message=%s file=%s expected_old_text=%s actual_old_text=%s",
                tool_name,
                len(errors),
                code,
                message,
                file_path,
                expected_short,
                actual_short,
            )
            return
        LOG.warning(
            "%s errors: count=%s first_code=%s first_message=%s file=%s",
            tool_name,
            len(errors),
            code,
            message,
            file_path,
        )
        return
    LOG.warning("%s errors: count=%s", tool_name, len(errors))


def _log_llm_error(exc: Exception) -> None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = None
    if response is not None:
        try:
            body = response.text
        except Exception:
            try:
                body = response.read().decode("utf-8", errors="replace")
            except Exception:
                body = None
    if body is not None:
        LOG.error(
            "LLM request failed: status=%s body=%s",
            status,
            body,
        )
    else:
        LOG.error("LLM request failed: %s", exc)


def _trim_messages(
    messages: list[Any],
    *,
    max_messages: int = 40,
    max_tool_chars: int = 4000,
    max_check_message_chars: int = 4000,
) -> list[Any]:
    if max_messages > 0 and len(messages) > max_messages:
        messages = _trim_messages_preserve_tools(messages, max_messages=max_messages)
    for msg in messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, str):
            continue
        trimmed: str | None = None
        if hasattr(msg, "tool_call_id"):
            if max_tool_chars <= 0 or len(content) <= max_tool_chars:
                continue
            trimmed = _truncate_text_tail(content, max_tool_chars)
        elif _is_check_log_message(content):
            if max_check_message_chars <= 0 or len(content) <= max_check_message_chars:
                continue
            trimmed = _truncate_text_tail(content, max_check_message_chars)
        if trimmed is None:
            continue
        try:
            msg.content = trimmed
        except Exception:
            continue
    return messages


def _trim_messages_preserve_tools(
    messages: list[Any], *, max_messages: int
) -> list[Any]:
    if max_messages <= 0 or len(messages) <= max_messages:
        return messages
    first = messages[:1]
    remaining = messages[1:]
    needed_tool_ids: set[str] = set()
    collected: list[Any] = []

    for msg in reversed(remaining):
        if len(collected) >= max_messages - len(first) and not needed_tool_ids:
            break
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id:
            needed_tool_ids.add(str(tool_call_id))
            collected.append(msg)
            continue
        tool_calls = _extract_tool_calls(msg)
        if tool_calls:
            ids = [call.get("id") for call in tool_calls]
            ids_str = {str(item) for item in ids if item}
            if needed_tool_ids and needed_tool_ids.intersection(ids_str):
                needed_tool_ids.difference_update(ids_str)
                collected.append(msg)
                continue
        collected.append(msg)

    if needed_tool_ids:
        for msg in reversed(remaining):
            tool_calls = _extract_tool_calls(msg)
            if not tool_calls:
                continue
            ids = [call.get("id") for call in tool_calls]
            ids_str = {str(item) for item in ids if item}
            if needed_tool_ids.intersection(ids_str):
                collected.append(msg)
                needed_tool_ids.difference_update(ids_str)
                if not needed_tool_ids:
                    break

    trimmed = first + list(reversed(collected))
    return trimmed[-max_messages:] if len(trimmed) > max_messages else trimmed


def _summarize_schema(schema: object) -> object:
    if not isinstance(schema, dict):
        return {"type": type(schema).__name__}
    summary: dict[str, object] = {}
    if "type" in schema:
        summary["type"] = schema.get("type")
    if "required" in schema:
        summary["required"] = schema.get("required")
    if "additionalProperties" in schema:
        summary["additionalProperties"] = schema.get("additionalProperties")
    if "enum" in schema:
        summary["enum"] = schema.get("enum")
    if "const" in schema:
        summary["const"] = schema.get("const")
    if "properties" in schema and isinstance(schema.get("properties"), dict):
        props = {}
        for key, value in schema["properties"].items():
            props[key] = _summarize_schema(value)
        summary["properties"] = props
    if "items" in schema:
        summary["items"] = _summarize_schema(schema.get("items"))
    if "anyOf" in schema:
        summary["anyOf"] = [_summarize_schema(item) for item in schema.get("anyOf", [])]
    if "oneOf" in schema:
        summary["oneOf"] = [_summarize_schema(item) for item in schema.get("oneOf", [])]
    if "allOf" in schema:
        summary["allOf"] = [_summarize_schema(item) for item in schema.get("allOf", [])]
    if "minimum" in schema:
        summary["minimum"] = schema.get("minimum")
    if "minLength" in schema:
        summary["minLength"] = schema.get("minLength")
    if "minItems" in schema:
        summary["minItems"] = schema.get("minItems")
    return summary


def _is_check_log_message(content: str) -> bool:
    return (
        content.startswith("run_checks result:")
        or content.startswith("run_checks summary:")
        or content.startswith("Checks failed. Logs:")
    )


def _shorten_log_text(value: object, *, max_chars: int = 220) -> str | None:
    if not isinstance(value, str):
        return None
    escaped = value.replace("\r", "\\r").replace("\n", "\\n")
    if len(escaped) <= max_chars:
        return escaped
    return f"{escaped[:max_chars]}...[truncated]"


def _truncate_text_tail(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"[truncated {dropped} chars]\n{text[-max_chars:]}"

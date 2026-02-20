from __future__ import annotations

from collections.abc import Callable

from megafix.code_agent.check_tools import TOOL_HANDLERS as CHECK_TOOL_HANDLERS
from megafix.code_agent.context_tools import TOOL_HANDLERS as CONTEXT_TOOL_HANDLERS
from megafix.code_agent.edit_tools import TOOL_HANDLERS as EDIT_TOOL_HANDLERS
from megafix.code_agent.patch_tools import TOOL_HANDLERS as PATCH_TOOL_HANDLERS
from megafix.shared.settings import get_settings
from megafix.shared.tool_schemas import get_tool_definitions as build_tool_defs

ToolHandler = Callable[..., object]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    **CHECK_TOOL_HANDLERS,
    **CONTEXT_TOOL_HANDLERS,
    **EDIT_TOOL_HANDLERS,
    **PATCH_TOOL_HANDLERS,
}


def get_tool_definitions() -> list[dict[str, object]]:
    settings = get_settings()
    return build_tool_defs(strict=settings.tool_schema_strict)


def get_tool_handler(name: str) -> ToolHandler:
    return TOOL_HANDLERS[name]

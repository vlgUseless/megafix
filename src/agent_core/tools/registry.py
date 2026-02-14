from __future__ import annotations

from collections.abc import Callable

from agent_core.schemas.tool_schemas import get_tool_definitions as build_tool_defs
from agent_core.settings import get_settings
from agent_core.tools.check_tools import TOOL_HANDLERS as CHECK_TOOL_HANDLERS
from agent_core.tools.context_tools import TOOL_HANDLERS as CONTEXT_TOOL_HANDLERS
from agent_core.tools.edit_tools import TOOL_HANDLERS as EDIT_TOOL_HANDLERS
from agent_core.tools.patch_tools import TOOL_HANDLERS as PATCH_TOOL_HANDLERS

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

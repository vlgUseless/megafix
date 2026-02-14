from __future__ import annotations

import copy

REPO_PATCH_ITEM_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "unified_diff": {"type": "string", "minLength": 1},
    },
    "required": ["path", "unified_diff"],
}

REPO_PATCHES_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "patches": {
            "type": "array",
            "minItems": 1,
            "items": REPO_PATCH_ITEM_SCHEMA,
        }
    },
    "required": ["patches"],
}

STRUCTURED_EDIT_ITEM_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Single structured edit operation. "
        "For replace_range/delete_range use start_line+end_line, for insert_after use "
        "line, for create_file all line fields must be null. "
        "new_text is required by runtime for replace_range/insert_after/create_file "
        "and ignored for delete_range. "
        "expected_old_text must match current repository content exactly; for "
        "create_file it must be an empty string."
    ),
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "op": {
            "type": "string",
            "enum": ["replace_range", "insert_after", "delete_range", "create_file"],
        },
        "start_line": {"type": ["integer", "null"], "minimum": 1},
        "end_line": {"type": ["integer", "null"], "minimum": 1},
        "line": {"type": ["integer", "null"], "minimum": 1},
        "new_text": {"type": ["string", "null"]},
        "expected_old_text": {"type": "string"},
    },
    "required": [
        "path",
        "op",
        "start_line",
        "end_line",
        "line",
        "new_text",
        "expected_old_text",
    ],
}


REPO_EDITS_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Structured edit contract returned by LLM. "
        "The runtime engine applies these edits and generates unified diff itself."
    ),
    "properties": {
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": STRUCTURED_EDIT_ITEM_SCHEMA,
        }
    },
    "required": ["edits"],
}

REPO_LIST_FILES_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

REPO_GREP_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "glob": {"type": ["string", "null"]},
        "max_results": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["query", "glob", "max_results"],
}

REPO_READ_FILE_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    "required": ["path", "start_line", "end_line"],
}

RUN_CHECKS_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "commands": {
            "type": ["array", "null"],
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        }
    },
    "required": ["commands"],
}

PATCH_ERROR_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "file_path": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"]},
        "details": {
            "type": ["object", "null"],
            "additionalProperties": True,
        },
    },
    "required": ["code", "message", "file_path", "line", "details"],
}

PATCH_FILE_STATS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string"},
        "status": {"type": "string"},
        "additions": {"type": "integer"},
        "deletions": {"type": "integer"},
        "deletion_ratio": {"type": ["number", "null"]},
    },
    "required": ["path", "status", "additions", "deletions", "deletion_ratio"],
}

PATCH_STATS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "total_additions": {"type": "integer"},
        "total_deletions": {"type": "integer"},
        "files_touched": {"type": "integer"},
        "per_file": {
            "type": "array",
            "items": PATCH_FILE_STATS_SCHEMA,
        },
    },
    "required": [
        "total_additions",
        "total_deletions",
        "files_touched",
        "per_file",
    ],
}

EDIT_OPERATION_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "path": {"type": ["string", "null"]},
        "op": {"type": ["string", "null"]},
        "status": {
            "type": "string",
            "enum": ["pending", "validated", "applied", "error", "skipped"],
        },
        "error": {"anyOf": [PATCH_ERROR_SCHEMA, {"type": "null"}]},
    },
    "required": ["index", "path", "op", "status", "error"],
}

REPO_PROPOSE_PATCHES_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": PATCH_ERROR_SCHEMA,
        },
        "stats": {"anyOf": [PATCH_STATS_SCHEMA, {"type": "null"}]},
    },
    "required": ["accepted", "errors", "stats"],
}

REPO_APPLY_PATCHES_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "applied": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": PATCH_ERROR_SCHEMA,
        },
        "stats": {"anyOf": [PATCH_STATS_SCHEMA, {"type": "null"}]},
    },
    "required": ["applied", "errors", "stats"],
}

REPO_PROPOSE_EDITS_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accepted": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": PATCH_ERROR_SCHEMA,
        },
        "stats": {"anyOf": [PATCH_STATS_SCHEMA, {"type": "null"}]},
        "patches": {
            "type": "array",
            "items": REPO_PATCH_ITEM_SCHEMA,
        },
        "operation_results": {
            "type": "array",
            "items": EDIT_OPERATION_RESULT_SCHEMA,
        },
    },
    "required": ["accepted", "errors", "stats", "patches", "operation_results"],
}

REPO_APPLY_EDITS_RESULT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "applied": {"type": "boolean"},
        "errors": {
            "type": "array",
            "items": PATCH_ERROR_SCHEMA,
        },
        "stats": {"anyOf": [PATCH_STATS_SCHEMA, {"type": "null"}]},
        "patches": {
            "type": "array",
            "items": REPO_PATCH_ITEM_SCHEMA,
        },
        "operation_results": {
            "type": "array",
            "items": EDIT_OPERATION_RESULT_SCHEMA,
        },
    },
    "required": ["applied", "errors", "stats", "patches", "operation_results"],
}

REPO_PROPOSE_EDITS_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_propose_edits",
        "description": (
            "Validate structured edits, generate unified diffs, then run policy checks "
            "and git apply --check on generated patches."
        ),
        "parameters": REPO_EDITS_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_APPLY_EDITS_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_apply_edits",
        "description": (
            "Apply structured edits after validation and policy checks; returns generated "
            "unified diffs."
        ),
        "parameters": REPO_EDITS_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_PROPOSE_PATCHES_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_propose_patches",
        "description": "Validate unified diffs with policy checks and git apply --check.",
        "parameters": REPO_PATCHES_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_LIST_FILES_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_list_files",
        "description": "List tracked files in the repository.",
        "parameters": REPO_LIST_FILES_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_GREP_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_grep",
        "description": "Search repository files for a query with optional glob filter.",
        "parameters": REPO_GREP_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_READ_FILE_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_read_file",
        "description": "Read a file slice by line range.",
        "parameters": REPO_READ_FILE_INPUT_SCHEMA,
        "strict": True,
    },
}

REPO_APPLY_PATCHES_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "repo_apply_patches",
        "description": "Apply unified diffs after validation and policy checks.",
        "parameters": REPO_PATCHES_INPUT_SCHEMA,
        "strict": True,
    },
}

TOOL_DEFINITIONS: list[dict[str, object]] = [
    REPO_LIST_FILES_TOOL,
    REPO_GREP_TOOL,
    REPO_READ_FILE_TOOL,
    REPO_PROPOSE_EDITS_TOOL,
    REPO_APPLY_EDITS_TOOL,
    REPO_PROPOSE_PATCHES_TOOL,
    REPO_APPLY_PATCHES_TOOL,
]


def get_tool_definitions(*, strict: bool = True) -> list[dict[str, object]]:
    definitions = copy.deepcopy(TOOL_DEFINITIONS)
    if strict:
        return definitions
    for tool in definitions:
        function = tool.get("function")
        if isinstance(function, dict):
            function.pop("strict", None)
    return definitions


def get_structured_edit_contract_schema() -> dict[str, object]:
    return copy.deepcopy(REPO_EDITS_INPUT_SCHEMA)

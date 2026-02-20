from __future__ import annotations

from megafix.shared.tool_schemas import (
    REPO_APPLY_EDITS_RESULT_SCHEMA,
    REPO_EDITS_INPUT_SCHEMA,
    REPO_PROPOSE_EDITS_RESULT_SCHEMA,
    TOOL_DEFINITIONS,
    get_structured_edit_contract_schema,
)


def test_structured_edit_contract_has_top_level_edits_array() -> None:
    schema = get_structured_edit_contract_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["edits"]
    edits = schema["properties"]["edits"]
    assert edits["type"] == "array"
    assert edits["minItems"] == 1


def test_structured_edit_contract_supports_expected_operations() -> None:
    item_schema = REPO_EDITS_INPUT_SCHEMA["properties"]["edits"]["items"]
    ops = set(item_schema["properties"]["op"]["enum"])
    assert ops == {"replace_range", "insert_after", "delete_range", "create_file"}


def test_structured_edit_contract_requires_all_fields() -> None:
    item_schema = REPO_EDITS_INPUT_SCHEMA["properties"]["edits"]["items"]
    assert set(item_schema["required"]) == {
        "path",
        "op",
        "start_line",
        "end_line",
        "line",
        "new_text",
        "expected_old_text",
    }


def test_structured_edit_contract_marks_op_specific_fields_nullable() -> None:
    item_schema = REPO_EDITS_INPUT_SCHEMA["properties"]["edits"]["items"]
    props = item_schema["properties"]
    assert props["start_line"]["type"] == ["integer", "null"]
    assert props["end_line"]["type"] == ["integer", "null"]
    assert props["line"]["type"] == ["integer", "null"]
    assert props["new_text"]["type"] == ["string", "null"]


def test_tool_definitions_prioritize_edit_tools_with_patch_fallback() -> None:
    names = [tool["function"]["name"] for tool in TOOL_DEFINITIONS]
    assert "repo_propose_edits" in names
    assert "repo_apply_edits" in names
    assert "repo_propose_patches" in names
    assert "repo_apply_patches" in names
    assert names.index("repo_propose_edits") < names.index("repo_propose_patches")
    assert names.index("repo_apply_edits") < names.index("repo_apply_patches")


def test_edit_tool_result_schema_includes_operation_results() -> None:
    assert "operation_results" in REPO_PROPOSE_EDITS_RESULT_SCHEMA["required"]
    assert "operation_results" in REPO_APPLY_EDITS_RESULT_SCHEMA["required"]
    op_results = REPO_PROPOSE_EDITS_RESULT_SCHEMA["properties"]["operation_results"]
    assert op_results["type"] == "array"
    item = op_results["items"]
    assert item["type"] == "object"
    assert "status" in item["properties"]


def test_all_tool_parameter_object_properties_are_required() -> None:
    def _walk(schema: object, path: str, errors: list[str]) -> None:
        if not isinstance(schema, dict):
            return
        if (
            schema.get("type") == "object"
            and isinstance(schema.get("properties"), dict)
            and schema["properties"]
        ):
            props = set(schema["properties"].keys())
            required = schema.get("required")
            req = set(required) if isinstance(required, list) else set()
            missing = sorted(props - req)
            if missing:
                errors.append(f"{path}: missing required for {missing}")

        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                _walk(value, f"{path}.properties.{key}", errors)

        _walk(schema.get("items"), f"{path}.items", errors)

        for key in ("anyOf", "oneOf", "allOf"):
            branches = schema.get(key)
            if not isinstance(branches, list):
                continue
            for idx, value in enumerate(branches):
                _walk(value, f"{path}.{key}[{idx}]", errors)

    errors: list[str] = []
    for tool in TOOL_DEFINITIONS:
        function = tool["function"]
        name = function["name"]
        _walk(function["parameters"], f"{name}.parameters", errors)

    assert not errors, "\n".join(errors)

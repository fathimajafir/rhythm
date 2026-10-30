"""Shared JSON-Schema subset validator.

Kept free of any package dependencies so both the tool registry and the model
output validator can use it without importing each other.
"""
from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate(schema: dict[str, Any], value: Any, path: str = "") -> list[str]:
    """Validate the subset of JSON Schema this project actually uses."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected and expected in _TYPES and not isinstance(value, _TYPES[expected]):
        # bool is an int in Python; keep the distinction honest
        if not (expected in ("integer", "number") and isinstance(value, bool)):
            return [f"{path or 'value'}: expected {expected}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or 'value'}: must be one of {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{path}: does not match required format")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"missing required field: {path + '.' if path else ''}{key}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value and value[key] is not None:
                errors.extend(validate(sub, value[key], f"{path + '.' if path else ''}{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                errors.append(f"unexpected fields: {sorted(extra)}")

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(schema["items"], item, f"{path}[{i}]"))
    return errors

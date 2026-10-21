"""Serialization helpers for MCP tool results returned to evaluation providers."""

from dataclasses import asdict, is_dataclass
import json
from typing import Any


def _plain_data(value: Any) -> Any:
    """Convert SDK objects and content blocks into JSON-safe data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_data(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain_data(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _plain_data(asdict(value))
    if hasattr(value, "__dict__"):
        return {
            str(k): _plain_data(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return str(value)


def _content_type(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("type")
    else:
        value = getattr(block, "type", None)
    return str(value) if value is not None else None


def _content_text(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("text")
    else:
        value = getattr(block, "text", None)
    return str(value) if value is not None else None


def serialize_tool_result(tool_result: Any) -> str:
    """Return provider-safe text for MCP content blocks and arbitrary results.

    MCP clients commonly return a list of typed content block objects. Passing
    those directly to json.dumps can fail because SDK objects are not plain JSON
    data. Text-only blocks are joined for readability; mixed/non-text content is
    emitted as JSON-safe structured data.
    """
    if isinstance(tool_result, list):
        text_blocks = [
            _content_text(block)
            for block in tool_result
            if _content_type(block) == "text" and _content_text(block) is not None
        ]
        if len(text_blocks) == len(tool_result):
            return "\n".join(text_blocks)

    plain = _plain_data(tool_result)
    if isinstance(plain, str):
        return plain
    return json.dumps(plain, ensure_ascii=True, separators=(",", ":"))

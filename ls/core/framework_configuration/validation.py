"""Validate preference contracts locally; never fetch schemas or grant authority."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator


CONTEXT_PREFERENCES = {"freshness", "chunking", "retrieval", "worker", "logging"}


def _partial(schema):
    if isinstance(schema, dict):
        return {key: _partial(value) for key, value in schema.items() if key != "required"}
    if isinstance(schema, list):
        return [_partial(value) for value in schema]
    return schema


def schema_document(source: Path) -> dict:
    """Inline existing owners so the exported schema is self-contained and offline."""
    schema = json.loads((source / "ls/config/framework.schema.json").read_text())
    install = json.loads((source / "ls/config/install.schema.json").read_text())
    install.pop("$id", None)
    schema["properties"]["installation"]["allOf"][0] = install
    context = json.loads((source / "ls/skills/ls-context-index-search/schemas/config.schema.json").read_text())
    context = _partial(context["properties"]["context_index"])
    context["properties"] = {k: v for k, v in context["properties"].items() if k in CONTEXT_PREFERENCES}
    context["additionalProperties"] = False
    schema["properties"]["context_index"] = context
    return schema


def validate_document(document: dict, source: Path, *, scope: str) -> None:
    schema = schema_document(source)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.path)) or "<root>"
        # Do not interpolate arbitrary input values, which may be secret mistakes.
        raise ValueError(f"Invalid framework configuration at {field}: {error.validator} constraint failed")
    if "testing" in document:
        from ..test_workers import validate_testing_configuration
        validate_testing_configuration(document["testing"])
    if "installation" in document:
        from ..config import parse_install_config
        parse_install_config(document["installation"])
    seen = set()
    for item in document.get("application_profiles", []):
        identity = (item["application"], item["name"])
        if identity in seen:
            raise ValueError("Duplicate application profile in framework configuration")
        seen.add(identity)
        from ..application_profiles.service import ApplicationProfileError, validate_profile
        try:
            validate_profile(*identity, copy.deepcopy(item["settings"]), source_root=source)
        except ApplicationProfileError:
            raise ValueError("Invalid application profile selection or settings in framework configuration") from None

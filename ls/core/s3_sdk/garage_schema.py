"""Extract the approved Garage Admin API contract without unrelated cluster APIs."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

SOURCE_URL = "https://garagehq.deuxfleurs.fr/api/garage-admin-v2.json"
SOURCE_SHA256 = "07ffb2d0febbf386747eed0232b1a97931d323182f11e77537519620e6eb10d8"
OPERATIONS = frozenset("ListBuckets GetBucketInfo CreateBucket DeleteBucket UpdateBucket ListKeys GetKeyInfo CreateKey UpdateKey ImportKey DeleteKey AllowBucketKey DenyBucketKey AddBucketAlias RemoveBucketAlias GetClusterHealth".split())
DISCARD = {"description", "summary", "examples", "example", "externalDocs", "tags"}


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip(item) for key, item in value.items() if key not in DISCARD}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def extract(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("Garage source schema hash differs from the reviewed contract")
    source = json.loads(raw)
    paths = {path: {method: _strip(op) for method, op in methods.items() if op.get("operationId") in OPERATIONS} for path, methods in source["paths"].items()}
    paths = {path: methods for path, methods in paths.items() if methods}
    definitions: dict[str, Any] = {}
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].removeprefix("#/components/schemas/")
                if name not in definitions:
                    definitions[name] = _strip(source["components"]["schemas"][name])
                    visit(definitions[name])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(paths)
    result = {"openapi": source["openapi"], "info": source["info"], "x-source": {"url": SOURCE_URL, "sha256": SOURCE_SHA256, "projection": "selected original operation schemas; documentation text removed"}, "paths": paths, "components": {"schemas": definitions}}
    return (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()


def generate(root: Path, source: Path, *, check: bool = False) -> None:
    raw = source.read_bytes()
    expected = extract(raw)
    base = root / "ls/skills/ls-garage-s3-admin/references"
    # Preserve the exact licensed upstream source with the modified projection.
    # Fixed gzip metadata makes both installed and release artifacts reproducible.
    outputs = {
        base / "admin-api-v2.schema.json": expected,
        base / "upstream-admin-api-v2.json.gz": gzip.compress(raw, mtime=0),
    }
    for destination in outputs:
        if any(path.is_symlink() for path in (destination, *destination.parents)):
            raise ValueError("refusing a symlink schema destination")
        if destination.exists() and not destination.is_file():
            raise ValueError("schema destination must be a regular file")
    if check:
        for destination, content in outputs.items():
            if not destination.exists() or destination.read_bytes() != content:
                raise ValueError("Garage extracted schema or upstream source drift")
    else:
        base.mkdir(parents=True, exist_ok=True)
        for destination, content in outputs.items():
            destination.write_bytes(content)

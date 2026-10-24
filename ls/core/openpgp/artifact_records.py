"""Canonical protected records for LocalSetup Secure Container schema one.

These records carry claims, not authority. The enclosing OpenPGP signatures,
exact policy, framing, and local trust must be verified separately.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata
from types import MappingProxyType
from typing import Any
import struct

from .container_format import (
    CONTAINER_FORMAT, ContainerLimits, MessageDescriptor, _MESSAGE_MARKER,
    _binary_block_serialized_length,
)
from .container_schema import PROTECTED_METADATA_SCHEMA, validate_protected_schema

# Retired unpublished markers remain recognizable by generic plaintext guards.
ARTIFACT_MAGIC = b"LSAR\x00\x01"
APPROVAL_MAGIC = b"LSAW\x00\x01"
_REQUIRED = frozenset({"segmented-messages-v1", "global-inventory-v1"})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_FIELD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PUBLIC_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_MESSAGE_MAGIC = b"LSSCM\x00\x01"
_MAX_PREFIX = 4096


class ArtifactRecordError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid protected container record")


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactProvenance:
    original_name: str | None = None
    original_path: str | None = None
    source_revision: str | None = None
    source_hashes: tuple[str, ...] = ()
    operational_metadata: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for value, maximum in ((self.original_name, 255), (self.original_path, 1024),
                               (self.source_revision, 128)):
            if value is not None:
                _private_text(value, maximum)
        if type(self.source_hashes) is not tuple or len(self.source_hashes) > 16 or any(
            type(value) is not str or _HEX64.fullmatch(value) is None
            for value in self.source_hashes
        ) or len(set(self.source_hashes)) != len(self.source_hashes):
            raise ArtifactRecordError()
        if not isinstance(self.operational_metadata, Mapping) or len(self.operational_metadata) > 16:
            raise ArtifactRecordError()
        copied: dict[str, str] = {}
        total = 0
        for key, value in self.operational_metadata.items():
            if type(key) is not str or _FIELD.fullmatch(key) is None:
                raise ArtifactRecordError()
            _private_text(value, 512)
            total += len(key.encode()) + len(value.encode())
            copied[key] = value
        if total > 4096:
            raise ArtifactRecordError()
        object.__setattr__(self, "operational_metadata", MappingProxyType(copied))


def _private_text(value: str, maximum: int) -> None:
    if (type(value) is not str or not value or len(value.encode("utf-8")) > maximum
            or unicodedata.normalize("NFC", value) != value
            or any(unicodedata.category(char) == "Cc" for char in value)):
        raise ArtifactRecordError()


def provenance_object(value: ArtifactProvenance) -> dict[str, Any]:
    if not isinstance(value, ArtifactProvenance):
        raise ArtifactRecordError()
    return {"original_name": value.original_name, "original_path": value.original_path,
            "source_revision": value.source_revision, "source_hashes": list(value.source_hashes),
            "operational_metadata": dict(value.operational_metadata)}


def parse_provenance(value: Any) -> ArtifactProvenance:
    if type(value) is not dict or set(value) != {
        "original_name", "original_path", "source_revision", "source_hashes", "operational_metadata"
    } or type(value["source_hashes"]) is not list:
        raise ArtifactRecordError()
    return ArtifactProvenance(
        original_name=value["original_name"], original_path=value["original_path"],
        source_revision=value["source_revision"], source_hashes=tuple(value["source_hashes"]),
        operational_metadata=value["operational_metadata"],
    )


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ArtifactRecordError() from None


def parse_canonical(data: bytes, *, maximum: int) -> dict[str, Any]:
    if type(data) is not bytes or not data or len(data) > maximum:
        raise ArtifactRecordError()
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if type(value) is not dict or canonical_bytes(value) != data:
            raise ArtifactRecordError()
        return value
    except (ValueError, UnicodeError):
        raise ArtifactRecordError() from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactRecordError()
        result[key] = value
    return result


def schema_envelope() -> dict[str, Any]:
    return {"schema": PROTECTED_METADATA_SCHEMA, "major": 1, "minor": 0,
            "required_features": sorted(_REQUIRED), "extensions": {}}


def validate_schema(value: Any) -> None:
    validate_protected_schema(value, supported_required_features=_REQUIRED)


def descriptor_object(value: MessageDescriptor) -> dict[str, Any]:
    return {"message_index": value.message_index, "offset": value.offset,
            "serialized_length": value.serialized_length,
            "ciphertext_length": value.ciphertext_length, "sha256": value.sha256}


def parse_descriptor(value: Any) -> MessageDescriptor:
    if type(value) is not dict or set(value) != {
        "message_index", "offset", "serialized_length", "ciphertext_length", "sha256"
    }:
        raise ArtifactRecordError()
    try:
        return MessageDescriptor(**value)
    except (TypeError, ValueError):
        raise ArtifactRecordError() from None


def validate_opaque_id(value: Any) -> str:
    if type(value) is not str or _HEX32.fullmatch(value) is None or value == "0" * 32:
        raise ArtifactRecordError()
    return value


def canonical_container_key(tenant_id: str, project_id: str,
                            publisher_opaque_id: str, artifact_id: str,
                            version_id: str, volume_id: str) -> str:
    if (type(tenant_id) is not str or _PUBLIC_NAMESPACE.fullmatch(tenant_id) is None
            or type(project_id) is not str or _PUBLIC_NAMESPACE.fullmatch(project_id) is None):
        raise ArtifactRecordError()
    return (f"lssc/v1/{tenant_id}/{project_id}/{validate_opaque_id(publisher_opaque_id)}/"
            f"{validate_opaque_id(artifact_id)}/{validate_opaque_id(version_id)}/"
            f"{validate_opaque_id(volume_id)}.lssc")


def parse_container_key(key: Any, artifact_id: str, version_id: str,
                        volume_id: str) -> tuple[str, str, str]:
    if type(key) is not str or len(key) > 512:
        raise ArtifactRecordError()
    parts = key.split("/")
    if len(parts) != 8 or parts[:2] != ["lssc", "v1"]:
        raise ArtifactRecordError()
    tenant, project, publisher = parts[2], parts[3], parts[4]
    if key != canonical_container_key(
        tenant, project, publisher, artifact_id, version_id, volume_id,
    ):
        raise ArtifactRecordError()
    return tenant, project, publisher


def validate_digest(value: Any) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ArtifactRecordError()
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def is_artifact_payload(payload: bytes) -> bool:
    return type(payload) is bytes and (
        payload.startswith((ARTIFACT_MAGIC, APPROVAL_MAGIC))
        or payload.startswith(b"format: " + CONTAINER_FORMAT.encode("ascii") + b"\n")
    )


def logical_commitment(logical: dict[str, Any], *, limits: ContainerLimits = ContainerLimits()) -> str:
    """Bind immutable logical identity without ciphertext or placement fields."""
    validate_logical(logical, limits=limits)
    return digest(logical)


def validate_logical(logical: Any, *, limits: ContainerLimits = ContainerLimits()) -> None:
    required = {"artifact_id", "version_id", "parent_versions", "origin", "audience",
                "scope", "purpose", "content_revision", "created_at", "expires_at", "files"}
    if type(logical) is not dict or set(logical) != required:
        raise ArtifactRecordError()
    validate_opaque_id(logical["artifact_id"])
    validate_opaque_id(logical["version_id"])
    parents = logical["parent_versions"]
    if (type(parents) is not list or len(parents) > 64
            or any(validate_opaque_id(value) != value for value in parents)
            or parents != sorted(set(parents))):
        raise ArtifactRecordError()
    origin = logical["origin"]
    if type(origin) is not dict or set(origin) != {"store_id", "revision", "epoch", "fingerprint"}:
        raise ArtifactRecordError()
    validate_digest(origin["store_id"])
    if (type(origin["revision"]) is not int or origin["revision"] < 0
            or type(origin["epoch"]) is not int or origin["epoch"] < 1
            or type(origin["fingerprint"]) is not str
            or re.fullmatch(r"[0-9A-F]{40}", origin["fingerprint"]) is None):
        raise ArtifactRecordError()
    for field_name, maximum in (("audience", 64), ("scope", 32)):
        value = logical[field_name]
        if (type(value) is not list or not value or len(value) > maximum
                or value != sorted(set(value)) or any(type(item) is not str for item in value)):
            raise ArtifactRecordError()
    if any(re.fullmatch(r"[0-9A-F]{40}", value) is None for value in logical["audience"]):
        raise ArtifactRecordError()
    for value in logical["scope"]:
        if _FIELD.fullmatch(value) is None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value) is None:
            raise ArtifactRecordError()
    for name in ("purpose", "content_revision"):
        value = logical[name]
        if type(value) is not str or not value or len(value) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+/-]*", value) is None:
            raise ArtifactRecordError()
    if (type(logical["created_at"]) is not int or type(logical["expires_at"]) is not int
            or logical["created_at"] < 0 or logical["expires_at"] <= logical["created_at"]
            or logical["expires_at"] - logical["created_at"] > 30 * 24 * 3600):
        raise ArtifactRecordError()
    files = logical["files"]
    if type(files) is not list or not files or len(files) > limits.max_files:
        raise ArtifactRecordError()
    seen: set[str] = set()
    for file in files:
        if type(file) is not dict or set(file) != {"file_id", "length", "sha256", "provenance"}:
            raise ArtifactRecordError()
        file_id = validate_opaque_id(file["file_id"])
        if file_id in seen or type(file["length"]) is not int or file["length"] < 0:
            raise ArtifactRecordError()
        seen.add(file_id)
        validate_digest(file["sha256"])
        parse_provenance(file["provenance"])


def validate_inventory(inventory: Any, logical: dict[str, Any], *,
                       limits: ContainerLimits = ContainerLimits()) -> None:
    """Check complete global segment coverage and exact framing expectations."""
    validate_logical(logical, limits=limits)
    if type(inventory) is not dict or set(inventory) != {"volumes"}:
        raise ArtifactRecordError()
    volumes = inventory["volumes"]
    if type(volumes) is not list or not volumes or len(volumes) > limits.max_volumes:
        raise ArtifactRecordError()
    volume_ids: set[str] = set()
    volume_keys: set[str] = set()
    namespace: tuple[str, str, str] | None = None
    by_file: dict[str, list[dict[str, Any]]] = {value["file_id"]: [] for value in logical["files"]}
    total_segments = 0
    for volume in volumes:
        if type(volume) is not dict or set(volume) != {"volume_id", "key", "messages"}:
            raise ArtifactRecordError()
        volume_id = validate_opaque_id(volume["volume_id"])
        key = volume["key"]
        observed_namespace = parse_container_key(
            key, logical["artifact_id"], logical["version_id"], volume_id,
        )
        if (volume_id in volume_ids or key in volume_keys
                or (namespace is not None and observed_namespace != namespace)):
            raise ArtifactRecordError()
        namespace = observed_namespace
        volume_ids.add(volume_id)
        volume_keys.add(key)
        messages = volume["messages"]
        if type(messages) is not list or not messages or len(messages) > limits.max_messages:
            raise ArtifactRecordError()
        total_segments += len(messages)
        if total_segments > limits.max_total_segments:
            raise ArtifactRecordError()
        expected_offset = len(b"messages:\n")
        for index, item in enumerate(messages):
            if type(item) is not dict or set(item) != {
                "file_id", "offset", "length", "segment_index", "segment_count",
                "plaintext_sha256", "descriptor"
            }:
                raise ArtifactRecordError()
            if item["file_id"] not in by_file:
                raise ArtifactRecordError()
            for number in ("offset", "length", "segment_index", "segment_count"):
                if type(item[number]) is not int or item[number] < 0:
                    raise ArtifactRecordError()
            if item["segment_count"] < 1 or item["segment_index"] >= item["segment_count"]:
                raise ArtifactRecordError()
            validate_digest(item["plaintext_sha256"])
            descriptor = parse_descriptor(item["descriptor"])
            if (descriptor.message_index != index or descriptor.offset != expected_offset
                    or descriptor.ciphertext_length <= 0
                    or descriptor.serialized_length != len(_MESSAGE_MARKER)
                    + _binary_block_serialized_length(
                        descriptor.ciphertext_length, indent=b"    ",
                    )):
                raise ArtifactRecordError()
            expected_offset += descriptor.serialized_length
            by_file[item["file_id"]].append(item)
    for file in logical["files"]:
        parts = sorted(by_file[file["file_id"]], key=lambda item: item["offset"])
        if not parts or len(parts) != parts[0]["segment_count"]:
            raise ArtifactRecordError()
        position = 0
        for index, part in enumerate(parts):
            if (part["offset"] != position or part["segment_index"] != index
                    or part["segment_count"] != len(parts)
                    or (part["length"] == 0 and file["length"] != 0)):
                raise ArtifactRecordError()
            position += part["length"]
        if position != file["length"]:
            raise ArtifactRecordError()


def encode_message_prefix(logical_sha256: str, *, file_id: str, offset: int,
                          segment_index: int, segment_count: int,
                          payload_length: int, plaintext_sha256: str) -> bytes:
    validate_digest(logical_sha256)
    validate_digest(plaintext_sha256)
    validate_opaque_id(file_id)
    if (any(type(value) is not int or value < 0 for value in
        (offset, segment_index, segment_count, payload_length))
            or segment_count < 1 or segment_index >= segment_count):
        raise ArtifactRecordError()
    prefix = canonical_bytes({"logical_sha256": logical_sha256, "file_id": file_id,
                              "offset": offset, "length": payload_length,
                              "segment_index": segment_index, "segment_count": segment_count,
                              "plaintext_sha256": plaintext_sha256})
    if len(prefix) > _MAX_PREFIX:
        raise ArtifactRecordError()
    return _MESSAGE_MAGIC + struct.pack(">I", len(prefix)) + prefix


def decode_message_prefix(value: bytes) -> dict[str, Any]:
    if (type(value) is not bytes or not value.startswith(_MESSAGE_MAGIC)
            or len(value) < len(_MESSAGE_MAGIC) + 4):
        raise ArtifactRecordError()
    position = len(_MESSAGE_MAGIC)
    length = struct.unpack(">I", value[position:position + 4])[0]
    if length < 1 or length > _MAX_PREFIX or len(value) != position + 4 + length:
        raise ArtifactRecordError()
    prefix = parse_canonical(value[position + 4:], maximum=_MAX_PREFIX)
    if set(prefix) != {"logical_sha256", "file_id", "offset", "length", "segment_index",
                       "segment_count", "plaintext_sha256"}:
        raise ArtifactRecordError()
    validate_digest(prefix["logical_sha256"])
    validate_opaque_id(prefix["file_id"])
    for name in ("offset", "length", "segment_index", "segment_count"):
        if type(prefix[name]) is not int or prefix[name] < 0:
            raise ArtifactRecordError()
    if (prefix["segment_count"] < 1 or prefix["segment_index"] >= prefix["segment_count"]
            or type(prefix["length"]) is not int or prefix["length"] < 0):
        raise ArtifactRecordError()
    validate_digest(prefix["plaintext_sha256"])
    return prefix

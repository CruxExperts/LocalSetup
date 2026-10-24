"""Operation-bound, one-use native input for explicit OpenPGP consumers.

This module binds a private form to the exact validated operation that will use
its input. It never launches GnuPG or persists trust. The existing terminal
dialog service owns native presentation and transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType
from enum import StrEnum
from typing import Any, Literal

from .contracts import (
    EnvelopePolicy,
    KeyCapability,
    KeyProfile,
    LocalTrust,
    OpenPGPContractError,
    normalize_fingerprint,
)
from .generation_models import KeyIdentity
from .key_records import KeyInspection, KeyRecord, _INSPECTION_SEAL
from ..terminal_ops.command_service import ShellCommandService
from ..terminal_ops.command_state import ShellCommandError, validate_identifier
from ..terminal_ops.dialog_model import Field, Form, Response

__all__ = [
    "InputOperation",
    "NativeInputContext",
    "NativeInputError",
    "NativeInputErrorCode",
    "describe_seal_operation",
    "describe_open_operation",
    "describe_seal_file_operation",
    "describe_open_file_operation",
    "describe_enrollment_operation",
    "describe_generation_operation",
]

_OPERATION_VERSION = 1
_MAX_PASSPHRASE_BYTES = 4096
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PURPOSES = {"seal-envelope", "open-envelope", "enroll-key", "generate-key"}
_SESSION_IDENTITY_KEYS = (
    "handle", "endpoint", "machine_generation", "owner_fingerprint", "native_session_id",
)
_INPUT_OPERATION_SEAL = object()


class NativeInputErrorCode(StrEnum):
    """Stable errors that never contain secret or caller-controlled text."""

    INVALID_OPERATION = "INVALID_OPERATION"
    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    INVALID_CONTEXT = "INVALID_CONTEXT"
    OWNER_UNAVAILABLE = "OWNER_UNAVAILABLE"
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    INPUT_ALREADY_USED = "INPUT_ALREADY_USED"
    ENROLLMENT_NOT_CONFIRMED = "ENROLLMENT_NOT_CONFIRMED"


class NativeInputError(RuntimeError):
    """Safe native-input failure containing only a stable code."""

    def __init__(self, code: str) -> None:
        if code not in {item.value for item in NativeInputErrorCode}:
            code = NativeInputErrorCode.INPUT_UNAVAILABLE
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class InputOperation:
    """Immutable digest and readable summary of one actual OpenPGP operation."""

    purpose: Literal["seal-envelope", "open-envelope", "enroll-key", "generate-key"]
    digest: str
    summary: str
    details: tuple[tuple[str, str], ...] = ()
    _home_identity: tuple[str, int, int] | None = field(default=None, repr=False, compare=False)
    _generation_selection: object | None = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._seal is not _INPUT_OPERATION_SEAL
            or self.purpose not in _PURPOSES
            or not isinstance(self.digest, str)
            or _DIGEST_RE.fullmatch(self.digest) is None
            or not isinstance(self.summary, str)
            or not self.summary
            or len(self.summary.encode("utf-8", "strict")) > 2048
            or not isinstance(self.details, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(part, str) for part in item)
                for item in self.details
            )
        ):
            raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)

    def __repr__(self) -> str:
        return f"InputOperation(purpose={self.purpose!r}, digest={self.digest!r}, summary={self.summary!r})"


def describe_seal_operation(
    *,
    payload: bytes,
    signer_fingerprint: str,
    recipient_fingerprints: Iterable[str],
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
    disable_compression: bool = False,
) -> InputOperation:
    """Describe the exact payload, participants, trust and verified GnuPG home."""
    if type(payload) is not bytes or type(disable_compression) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    signer = _fingerprint(signer_fingerprint)
    recipients = _fingerprint_set(recipient_fingerprints, nonempty=True)
    trust = _trust_set(local_trust)
    home = _verified_home_identity(gnupg_home)
    bindings = {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_length": len(payload),
        "signer_fingerprint": signer,
        "recipient_fingerprints": sorted(recipients),
        "local_trust_fingerprints": sorted(trust),
        "disable_compression": disable_compression,
        "gnupg_home": {"path": home[0], "device": home[1], "inode": home[2]},
    }
    summary = (
        f"Sign and seal {len(payload)} bytes as {signer}; recipients: "
        f"{_display_set(recipients)}; local trust pins: {_display_set(trust)}; "
        f"compression {'disabled' if disable_compression else 'enabled'}."
    )
    details = (
        ("payload_length", str(len(payload))),
        ("payload_sha256", bindings["payload_sha256"]),
        ("signer", signer),
        ("recipients", _display_set(recipients)),
        ("local_trust", _display_set(trust)),
        ("compression", "disabled" if disable_compression else "enabled"),
        ("gnupg_home_identity", _home_display_id(home)),
    )
    return _operation("seal-envelope", bindings, summary, details, home)


def describe_open_operation(
    *,
    serialized: bytes,
    policy: EnvelopePolicy,
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
) -> InputOperation:
    """Describe exact serialized bytes and every normalized opening policy pin."""
    if type(serialized) is not bytes or not isinstance(policy, EnvelopePolicy):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    trust = _trust_set(local_trust)
    home = _verified_home_identity(gnupg_home)
    policy_bindings = {
        "expected_signers": sorted(_fingerprint_set(policy.expected_signers, nonempty=True)),
        "expected_recipients": sorted(_fingerprint_set(policy.expected_recipients, nonempty=True)),
        "owner_fingerprint": (
            None if policy.owner_fingerprint is None else _fingerprint(policy.owner_fingerprint)
        ),
        "publisher_fingerprint": (
            None if policy.publisher_fingerprint is None else _fingerprint(policy.publisher_fingerprint)
        ),
    }
    bindings = {
        "serialized_sha256": hashlib.sha256(serialized).hexdigest(),
        "serialized_length": len(serialized),
        "policy": policy_bindings,
        "local_trust_fingerprints": sorted(trust),
        "gnupg_home": {"path": home[0], "device": home[1], "inode": home[2]},
    }
    summary = (
        f"Open {len(serialized)} serialized bytes; allowed signers: "
        f"{_display_set(policy_bindings['expected_signers'])}; allowed recipients: "
        f"{_display_set(policy_bindings['expected_recipients'])}; local trust pins: "
        f"{_display_set(trust)}; owner pin: {policy_bindings['owner_fingerprint'] or 'none'}; "
        f"publisher pin: {policy_bindings['publisher_fingerprint'] or 'none'}."
    )
    details = (
        ("serialized_length", str(len(serialized))),
        ("serialized_sha256", bindings["serialized_sha256"]),
        ("expected_signers", _display_set(policy_bindings["expected_signers"])),
        ("expected_recipients", _display_set(policy_bindings["expected_recipients"])),
        ("owner_pin", policy_bindings["owner_fingerprint"] or "none"),
        ("publisher_pin", policy_bindings["publisher_fingerprint"] or "none"),
        ("local_trust", _display_set(trust)),
        ("gnupg_home_identity", _home_display_id(home)),
    )
    return _operation("open-envelope", bindings, summary, details, home)


def describe_seal_file_operation(
    *,
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    limits: "FileEnvelopeLimits",
    signer_fingerprint: str,
    recipient_fingerprints: Iterable[str],
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
    disable_compression: bool = False,
) -> InputOperation:
    """Describe one exact anchored file seal and its validated OpenPGP inputs."""
    if type(disable_compression) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    binding = _inspect_file_binding(
        source_path, output_path, limits=limits, private_source=True,
    )
    return _describe_seal_file_operation(
        binding,
        signer_fingerprint=signer_fingerprint,
        recipient_fingerprints=recipient_fingerprints,
        local_trust=local_trust,
        gnupg_home=gnupg_home,
        disable_compression=disable_compression,
    )


def describe_open_file_operation(
    *,
    envelope_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    limits: "FileEnvelopeLimits",
    policy: EnvelopePolicy,
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
    authorized: bool,
) -> InputOperation:
    """Describe one exact anchored file open and its complete authorization policy."""
    if type(authorized) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    binding = _inspect_file_binding(
        envelope_path, output_path, limits=limits, private_source=False,
    )
    return _describe_open_file_operation(
        binding,
        policy=policy,
        local_trust=local_trust,
        gnupg_home=gnupg_home,
        authorized=authorized,
    )


def _describe_seal_file_operation(
    binding: Mapping[str, Any],
    *,
    signer_fingerprint: str,
    recipient_fingerprints: Iterable[str],
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
    disable_compression: bool = False,
) -> InputOperation:
    """Build a seal descriptor from a binding made by the file-I/O owner."""
    if type(disable_compression) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    selected = _validated_file_binding(binding, expected_operation="seal")
    signer = _fingerprint(signer_fingerprint)
    recipients = _fingerprint_set(recipient_fingerprints, nonempty=True)
    trust = _trust_set(local_trust)
    home = _verified_home_identity(gnupg_home)
    bindings = {
        "file_operation": "seal",
        "file_binding": selected,
        "signer_fingerprint": signer,
        "recipient_fingerprints": sorted(recipients),
        "local_trust_fingerprints": sorted(trust),
        "disable_compression": disable_compression,
        "gnupg_home": {"path": home[0], "device": home[1], "inode": home[2]},
    }
    source = selected["source"]
    output = selected["output"]
    summary = (
        f"Sign and seal {source['length']} bytes from {_short_path(_display_path(source['path']))} "
        f"to {_short_path(_display_path(output['path']))}; "
        f"signer: {signer}; recipients: {_display_set(recipients)}; "
        f"local trust pins: {_display_set(trust)}; "
        f"compression {'disabled' if disable_compression else 'enabled'}."
    )
    details = (
        ("file_operation", "seal"),
        ("source_path", _display_path(source["path"])),
        ("source_length", str(source["length"])),
        ("source_sha256", source["sha256"]),
        ("output_path", _display_path(output["path"])),
        ("output_selection", output["selection"]),
        ("limits", _limits_display(selected["limits"])),
        ("signer", signer),
        ("recipients", _display_set(recipients)),
        ("local_trust", _display_set(trust)),
        ("compression", "disabled" if disable_compression else "enabled"),
        ("gnupg_home_identity", _home_display_id(home)),
    )
    return _operation("seal-envelope", bindings, summary, details, home)


def _describe_open_file_operation(
    binding: Mapping[str, Any],
    *,
    policy: EnvelopePolicy,
    local_trust: LocalTrust,
    gnupg_home: str | os.PathLike[str],
    authorized: bool,
) -> InputOperation:
    """Build an open descriptor from a binding made by the file-I/O owner."""
    if not isinstance(policy, EnvelopePolicy) or type(authorized) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    selected = _validated_file_binding(binding, expected_operation="open")
    trust = _trust_set(local_trust)
    home = _verified_home_identity(gnupg_home)
    policy_bindings = {
        "expected_signers": sorted(_fingerprint_set(policy.expected_signers, nonempty=True)),
        "expected_recipients": sorted(_fingerprint_set(policy.expected_recipients, nonempty=True)),
        "owner_fingerprint": (
            None if policy.owner_fingerprint is None else _fingerprint(policy.owner_fingerprint)
        ),
        "publisher_fingerprint": (
            None if policy.publisher_fingerprint is None else _fingerprint(policy.publisher_fingerprint)
        ),
    }
    bindings = {
        "file_operation": "open",
        "file_binding": selected,
        "policy": policy_bindings,
        "local_trust_fingerprints": sorted(trust),
        "authorized": authorized,
        "gnupg_home": {"path": home[0], "device": home[1], "inode": home[2]},
    }
    source = selected["source"]
    output = selected["output"]
    summary = (
        f"Open {source['length']} bytes from {_short_path(_display_path(source['path']))} "
        f"to {_short_path(_display_path(output['path']))}; "
        f"allowed signers: {_display_set(policy_bindings['expected_signers'])}; "
        f"allowed recipients: {_display_set(policy_bindings['expected_recipients'])}; "
        f"local trust pins: {_display_set(trust)}; "
        f"authorization {'granted' if authorized else 'not granted'}; "
        f"owner pin: {policy_bindings['owner_fingerprint'] or 'none'}; "
        f"publisher pin: {policy_bindings['publisher_fingerprint'] or 'none'}."
    )
    details = (
        ("file_operation", "open"),
        ("source_path", _display_path(source["path"])),
        ("source_length", str(source["length"])),
        ("source_sha256", source["sha256"]),
        ("output_path", _display_path(output["path"])),
        ("output_selection", output["selection"]),
        ("limits", _limits_display(selected["limits"])),
        ("expected_signers", _display_set(policy_bindings["expected_signers"])),
        ("expected_recipients", _display_set(policy_bindings["expected_recipients"])),
        ("owner_pin", policy_bindings["owner_fingerprint"] or "none"),
        ("publisher_pin", policy_bindings["publisher_fingerprint"] or "none"),
        ("local_trust", _display_set(trust)),
        ("authorized", "yes" if authorized else "no"),
        ("gnupg_home_identity", _home_display_id(home)),
    )
    return _operation("open-envelope", bindings, summary, details, home)


def _inspect_file_binding(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    limits: object,
    private_source: bool,
) -> Mapping[str, Any]:
    """Inspect file identities and bytes before any native form is displayed."""
    if type(private_source) is not bool:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    try:
        from .envelope_io import _inspect_native_file_binding

        binding = _inspect_native_file_binding(
            source_path, output_path, limits=limits, private_source=private_source,
        )
        return _validated_file_binding(binding, expected_operation="seal" if private_source else "open")
    except NativeInputError:
        raise
    except Exception:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _validated_file_binding(
    value: Mapping[str, Any], *, expected_operation: Literal["seal", "open"]
) -> dict[str, Any]:
    """Copy a helper binding into strict JSON data and check its file-operation shape."""
    try:
        if not isinstance(value, Mapping) or value.get("file_operation") != expected_operation:
            raise ValueError
        selected = json.loads(json.dumps(
            dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ))
        source = selected["source"]
        source_identity = source["identity"]
        source_parent = source["parent"]
        output = selected["output"]
        parent = output["parent"]
        limits = selected["limits"]
        required_identity = {"device", "inode", "uid", "mode", "size", "mtime_ns", "ctime_ns"}
        required_source_parent = {"path", "device", "inode", "uid", "mode"}
        required_parent = {"path", "device", "inode", "uid", "mode"}
        required_limits = {
            "max_plaintext_bytes", "max_envelope_bytes", "max_temporary_bytes",
            "max_diagnostics_bytes", "timeout_seconds",
        }
        if (
            not isinstance(source, dict)
            or not isinstance(source_identity, dict)
            or set(source_identity) != required_identity
            or not isinstance(source_parent, dict)
            or set(source_parent) != required_source_parent
            or not isinstance(output, dict)
            or not isinstance(parent, dict)
            or set(parent) != required_parent
            or not isinstance(limits, dict)
            or set(limits) != required_limits
            or not isinstance(source["path"], str)
            or not os.path.isabs(source["path"])
            or not isinstance(source_parent["path"], str)
            or not os.path.isabs(source_parent["path"])
            or os.path.dirname(source["path"]) != source_parent["path"]
            or not isinstance(source["sha256"], str)
            or _DIGEST_RE.fullmatch(source["sha256"]) is None
            or type(source["length"]) is not int
            or source["length"] < 0
            or not isinstance(output["path"], str)
            or not os.path.isabs(output["path"])
            or not isinstance(output["name"], str)
            or not output["name"]
            or os.path.basename(output["path"]) != output["name"]
            or os.path.dirname(output["path"]) != parent["path"]
            or output["selection"] != "absent"
            or not isinstance(parent["path"], str)
            or not os.path.isabs(parent["path"])
            or any(type(item) is not int or item < 0 for item in source_identity.values())
            or source_identity["size"] != source["length"]
            or any(type(source_parent[key]) is not int or source_parent[key] < 0
                   for key in ("device", "inode", "uid", "mode"))
            or any(type(parent[key]) is not int or parent[key] < 0 for key in ("device", "inode", "uid", "mode"))
            or any(type(limits[key]) is not int or limits[key] <= 0 for key in required_limits - {"timeout_seconds"})
            or type(limits["timeout_seconds"]) not in (int, float)
            or not math.isfinite(limits["timeout_seconds"])
            or not 0 < limits["timeout_seconds"] <= 3600
        ):
            raise ValueError
        return selected
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def describe_enrollment_operation(
    *,
    inspection: KeyInspection,
    expected_fingerprint: str,
    required_capabilities: Iterable[KeyCapability | str],
    profile: KeyProfile,
    local_trust: LocalTrust,
) -> InputOperation:
    """Describe all inspected key records and the exact local enrollment scope."""
    if (
        not isinstance(inspection, KeyInspection)
        or inspection._verification is not _INSPECTION_SEAL
        or not isinstance(profile, KeyProfile)
    ):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    expected = _fingerprint(expected_fingerprint)
    if expected != inspection.primary_fingerprint:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    capabilities = _capability_set(required_capabilities)
    trust = _trust_set(local_trust)
    records = [_record_binding(record) for record in inspection.records]
    bindings = {
        "inspection_records": records,
        "expected_fingerprint": expected,
        "required_capabilities": sorted(value.value for value in capabilities),
        "profile": {
            "algorithm": profile.algorithm,
            "bits": profile.bits,
            "validity_years": profile.validity_years,
        },
        "local_trust_fingerprints": sorted(trust),
        "enrollment_scope": "local-openpgp-trust",
    }
    capability_text = ", ".join(sorted(value.value for value in capabilities))
    roles = tuple(
        label
        for capability, label in (
            (KeyCapability.SIGN, "signer"),
            (KeyCapability.ENCRYPT, "recipient"),
        )
        if capability in capabilities
    )
    role_text = ", ".join(roles)
    summary = (
        f"Enroll {expected} as {role_text} in local OpenPGP trust "
        f"(capabilities: {capability_text}); "
        f"profile {profile.algorithm}-{profile.bits} for {profile.validity_years} years; "
        f"scope: this local trust store; existing pins: {_display_set(trust)}."
    )
    details = (
        ("fingerprint", expected),
        ("records", str(len(records))),
        ("capabilities", capability_text),
        ("roles", role_text),
        ("profile", f"{profile.algorithm}-{profile.bits}/{profile.validity_years}y"),
        ("local_trust", _display_set(trust)),
        ("scope", "local-openpgp-trust"),
    )
    return _operation("enroll-key", bindings, summary, details, None)


def describe_generation_operation(
    *,
    identity: KeyIdentity,
    profile: KeyProfile,
    keyring_home: str | os.PathLike[str],
) -> InputOperation:
    """Describe validated generation arguments and their read-only home selection."""
    from .generation_home import GenerationHomeSelection, inspect_generation_home

    if not isinstance(identity, KeyIdentity) or not isinstance(profile, KeyProfile):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    try:
        selection = inspect_generation_home(keyring_home)
        if not isinstance(selection, GenerationHomeSelection):
            raise ValueError
        selected = _generation_selection_binding(selection)
    except Exception:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None
    identity_binding = {
        "name": identity.name,
        "email": identity.email,
        "comment": identity.comment,
        "certification_notations": [list(item) for item in identity.certification_notations],
    }
    profile_binding = {
        "algorithm": profile.algorithm,
        "bits": profile.bits,
        "validity_years": profile.validity_years,
    }
    bindings = {
        "identity": identity_binding,
        "profile": profile_binding,
        "keyring_home_selection": selected,
    }
    identity_label = identity.name
    if identity.email:
        identity_label += f" <{identity.email}>"
    summary = (
        f"Generate a protected {profile.algorithm}-{profile.bits} key for {identity_label}; "
        f"valid for {profile.validity_years} years."
    )
    details = (
        ("identity_name", identity.name),
        ("identity_email", identity.email or "none"),
        ("identity_comment", identity.comment or "none"),
        ("profile", f"{profile.algorithm}-{profile.bits}/{profile.validity_years}y"),
        ("notation_count", str(len(identity.certification_notations))),
        ("notation_names", _display_set(item[0] for item in identity.certification_notations)),
        ("keyring_home", selected["path"]),
    )
    return _operation(
        "generate-key", bindings, summary, details, None, generation_selection=selection
    )


class NativeInputContext:
    """Bind one native form to an explicitly owned root session and one operation."""

    def __init__(
        self,
        *,
        service: ShellCommandService,
        handle: str,
        owner: Mapping[str, Any],
        operation_id: str,
        expected: InputOperation,
        deadline: float,
        validate_current: Callable[[], bool],
    ) -> None:
        if (
            not isinstance(service, ShellCommandService)
            or not isinstance(owner, Mapping)
            or not callable(validate_current)
            or not _is_operation(expected)
            or type(deadline) not in {int, float}
            or not math.isfinite(deadline)
            or not time.monotonic() < deadline
        ):
            raise NativeInputError(NativeInputErrorCode.INVALID_CONTEXT)
        try:
            self.operation_id = validate_identifier(operation_id, "operation_id")
            self.handle = validate_identifier(handle, "handle")
            self.owner = MappingProxyType(dict(owner))
        except (ShellCommandError, TypeError, ValueError):
            raise NativeInputError(NativeInputErrorCode.INVALID_CONTEXT) from None
        self.service = service
        self.expected = expected
        self.deadline = float(deadline)
        self._validate_current = validate_current
        self._status = "fresh"
        self._handoff: bytearray | None = None
        self._delivered = False
        self._lock = threading.RLock()
        self._consumer_lease: object | None = None
        self._consumer_thread: int | None = None
        try:
            _directory, session, operation = self.service._load(self.handle)
            self.service._authorize(session, self.owner)
            _caller, role, _slug, _fingerprint = self.service._family_caller(
                session, self.owner, require_root_live=True
            )
            if (
                operation is not None
                or role != "root"
                or session.get("layout_mode") != "canvas-v3"
                or session.get("state") != "ready"
                or self.service._verify(session).get("state") != "ready"
                or self._safe_validate_current() is not True
            ):
                raise ValueError
            self._session_identity = {
                key: session.get(key) for key in _SESSION_IDENTITY_KEYS
            }
        except Exception:
            raise NativeInputError(NativeInputErrorCode.OWNER_UNAVAILABLE) from None

    def __repr__(self) -> str:
        return f"<NativeInputContext purpose={self.expected.purpose!r} state={self._status!r}>"

    def claim(self, operation: InputOperation, *, deadline: float | None = None) -> object:
        """Reserve the entire one-use consumer call before it can request input.

        A rejected caller receives no lease and has no authority to finish the
        active consumer's context. No lock is held while the dialog or GnuPG runs.
        """
        self._require_operation(
            operation, ("seal-envelope", "open-envelope", "enroll-key", "generate-key")
        )
        with self._lock:
            if self._status != "fresh" or self._consumer_lease is not None:
                raise NativeInputError(NativeInputErrorCode.INPUT_ALREADY_USED)
            if deadline is not None:
                if type(deadline) not in {int, float} or not math.isfinite(deadline):
                    raise NativeInputError(NativeInputErrorCode.INVALID_CONTEXT)
                if time.monotonic() >= deadline:
                    raise NativeInputError(NativeInputErrorCode.DEADLINE_EXPIRED)
                # Only the winning consumer may shorten the input phase to its
                # whole-operation budget. A claim can never extend the deadline.
                self.deadline = min(self.deadline, float(deadline))
            lease = object()
            self._consumer_lease = lease
            self._consumer_thread = threading.get_ident()
            return lease

    def _reserve_request(self) -> None:
        with self._lock:
            self._require_consumer_thread()
            if self._status != "fresh":
                raise NativeInputError(NativeInputErrorCode.INPUT_ALREADY_USED)
            self._status = "requesting"

    def _require_consumer_thread(self) -> None:
        if (
            self._consumer_lease is not None
            and self._consumer_thread != threading.get_ident()
        ):
            raise NativeInputError(NativeInputErrorCode.INPUT_ALREADY_USED)

    def request_passphrase(self, operation: InputOperation) -> bytearray:
        """Request a single bounded password and transfer its owned buffer once."""
        self._require_operation(operation, ("seal-envelope", "open-envelope", "generate-key"))
        self._reserve_request()
        try:
            self._require_current(require_deadline=True)
            self._verify_operation_home(operation)
            title = {
                "seal-envelope": "Unlock signing key",
                "open-envelope": "Unlock encrypted content",
                "generate-key": "Create protected key",
            }[operation.purpose]
            fields = (
                Field(
                    "passphrase", "Passphrase", kind="password", required=True,
                    max_bytes=_MAX_PASSPHRASE_BYTES,
                ),
            )
            if operation.purpose == "generate-key":
                fields += (
                    Field(
                        "confirmation", "Confirm passphrase", kind="password", required=True,
                        max_bytes=_MAX_PASSPHRASE_BYTES, matches="passphrase",
                    ),
                )
            form = Form(
                title,
                fields,
                description=self._dialog_description(operation),
            )
            result = self.service.dialog(
                self.handle,
                operation_id=self.operation_id,
                form=form,
                deadline=self.deadline,
                validate_current=lambda: self._channel_current(),
                deliver=lambda response: self._capture_passphrase(response, operation),
                owner=self.owner,
            )
            self._require_delivered_result(result)
            self._require_current(require_deadline=True)
            self._verify_operation_home(operation)
            with self._lock:
                if self._status != "requesting" or not self._delivered or self._handoff is None:
                    raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE)
                secret = self._handoff
                self._handoff = None
                self._status = "consumed"
                return secret
        except NativeInputError:
            self._close_and_wipe()
            raise
        except Exception:
            self._close_and_wipe()
            raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE) from None

    def confirm_enrollment(
        self,
        operation: InputOperation,
        *,
        fingerprint: str,
        description: str,
    ) -> None:
        """Require the user to type the inspected full fingerprint once."""
        self._require_operation(operation, ("enroll-key",))
        try:
            selected = _fingerprint(fingerprint)
        except NativeInputError:
            raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None
        details = dict(operation.details)
        if (
            selected != details.get("fingerprint")
            or description != operation.summary
            or self.expected.purpose != "enroll-key"
        ):
            raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)
        self._reserve_request()
        expected_text = selected.encode("ascii")
        try:
            self._require_current(require_deadline=True)
            form = Form(
                "Enroll certificate",
                (Field(
                    "fingerprint", "Type the full fingerprint to confirm local trust",
                    kind="confirm", required=True, max_bytes=40,
                    confirmation=selected,
                ),),
                description=self._dialog_description(operation),
            )
            result = self.service.dialog(
                self.handle,
                operation_id=self.operation_id,
                form=form,
                deadline=self.deadline,
                validate_current=lambda: self._channel_current(),
                deliver=lambda response: self._capture_confirmation(response, expected_text),
                owner=self.owner,
            )
            self._require_delivered_result(result)
            self._require_current(require_deadline=True)
            with self._lock:
                if self._status != "requesting" or not self._delivered:
                    raise NativeInputError(NativeInputErrorCode.ENROLLMENT_NOT_CONFIRMED)
                self._status = "consumed"
        except NativeInputError:
            self._close_and_wipe()
            raise
        except Exception:
            self._close_and_wipe()
            raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE) from None

    def checkpoint(self, operation: InputOperation) -> None:
        """Check owner, descriptor and home identity before execution.

        The input deadline applies before a form is accepted. Once consumed,
        execution uses the consumer's own deadline and only owner/identity checks
        remain active.
        """
        self._require_operation(
            operation, ("seal-envelope", "open-envelope", "enroll-key", "generate-key")
        )
        with self._lock:
            self._require_consumer_thread()
            if self._status not in {"fresh", "consumed"}:
                raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE)
            require_deadline = self._status == "fresh"
        self._require_current(require_deadline=require_deadline)
        self._verify_operation_home(operation, full_generation_selection=require_deadline)
        with self._lock:
            if self._status not in {"fresh", "consumed"}:
                raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE)

    def finish(self, lease: object | None = None) -> None:
        """Close direct input, or release only the matching consumer's lease."""
        with self._lock:
            if self._consumer_lease is not None:
                if lease is not self._consumer_lease or self._consumer_thread != threading.get_ident():
                    return
            elif lease is not None:
                return
            self._close_and_wipe()
            self._consumer_lease = None
            self._consumer_thread = None

    def _capture_passphrase(self, response: Response, operation: InputOperation) -> bool:
        try:
            with self._lock:
                names = (
                    {"passphrase", "confirmation"}
                    if operation.purpose == "generate-key"
                    else {"passphrase"}
                )
                if (
                    self._status != "requesting"
                    or self._delivered
                    or self._handoff is not None
                    or not isinstance(response, Response)
                    or set(response.values) != names
                ):
                    return False
                value = response.values["passphrase"]
                if not isinstance(value, bytearray) or not _valid_passphrase(value):
                    return False
                if operation.purpose == "generate-key":
                    confirmation = response.values["confirmation"]
                    if (
                        not isinstance(confirmation, bytearray)
                        or not _valid_passphrase(confirmation)
                        or not hmac.compare_digest(value, confirmation)
                    ):
                        return False
                self._handoff = bytearray(value)
                self._delivered = True
                return True
        finally:
            if isinstance(response, Response):
                response.dispose()

    def _capture_confirmation(self, response: Response, expected: bytes) -> bool:
        try:
            with self._lock:
                if (
                    self._status != "requesting"
                    or self._delivered
                    or not isinstance(response, Response)
                    or set(response.values) != {"fingerprint"}
                ):
                    return False
                value = response.values["fingerprint"]
                if not isinstance(value, bytearray) or bytes(value) != expected:
                    return False
                self._delivered = True
                return True
        finally:
            if isinstance(response, Response):
                response.dispose()

    def _require_operation(
        self,
        operation: InputOperation,
        purposes: tuple[str, ...],
    ) -> None:
        if not _is_operation(operation) or operation.purpose not in purposes:
            raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
        if operation != self.expected:
            raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)

    def _require_delivered_result(self, result: object) -> None:
        if (
            not isinstance(result, Mapping)
            or result.get("state") != "delivered"
            or result.get("effect_certainty") != "known"
            or result.get("ok") is not True
            or result.get("cleanup_pending") is not False
            or result.get("handle") != self.handle
            or result.get("operation_id") != self.operation_id
        ):
            raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE)

    def _channel_current(self) -> bool:
        with self._lock:
            if self._status != "requesting":
                return False
        return self._current(require_deadline=True)

    def _require_current(self, *, require_deadline: bool) -> None:
        if not self._current(require_deadline=require_deadline):
            code = (
                NativeInputErrorCode.DEADLINE_EXPIRED
                if require_deadline and time.monotonic() >= self.deadline
                else NativeInputErrorCode.OWNER_UNAVAILABLE
            )
            raise NativeInputError(code)

    def _current(self, *, require_deadline: bool) -> bool:
        try:
            if require_deadline and time.monotonic() >= self.deadline:
                return False
            _directory, session, operation = self.service._load(self.handle)
            self.service._authorize(session, self.owner)
            _caller, role, _slug, _fingerprint = self.service._family_caller(
                session, self.owner, require_root_live=True
            )
            if (
                operation is not None
                or role != "root"
                or session.get("layout_mode") != "canvas-v3"
                or session.get("state") != "ready"
                or any(session.get(key) != value for key, value in self._session_identity.items())
                or self.service._verify(session).get("state") != "ready"
                or self._safe_validate_current() is not True
            ):
                return False
            return True
        except Exception:
            return False

    def _safe_validate_current(self) -> bool:
        try:
            return self._validate_current() is True
        except Exception:
            return False

    def _verify_operation_home(
        self, operation: InputOperation, *, full_generation_selection: bool = True
    ) -> None:
        if operation.purpose == "generate-key":
            selection = operation._generation_selection
            try:
                from .generation_home import (
                    check_generation_parent,
                    check_generation_selection,
                )

                if full_generation_selection:
                    check_generation_selection(selection)
                else:
                    check_generation_parent(selection)
            except Exception:
                raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH) from None
            return
        identity = operation._home_identity
        if identity is None:
            return
        try:
            if _verified_home_identity(identity[0]) != identity:
                raise ValueError
        except Exception:
            raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH) from None

    def _dialog_description(self, operation: InputOperation) -> str:
        details = dict(operation.details)
        if details.get("file_operation") == "seal":
            lines = [
                "Sign and encrypt the selected file.",
                f"Source: {details['source_path']}",
                f"Source size: {details['source_length']} bytes",
                f"Source SHA-256: {details['source_sha256']}",
                f"New output: {details['output_path']} ({details['output_selection']})",
                f"Limits: {details['limits']}",
                f"Signing key: {details['signer']}",
                f"Recipients: {details['recipients']}",
                f"Local trust pins: {details['local_trust']}",
                f"Compression: {details['compression']}",
            ]
        elif details.get("file_operation") == "open":
            lines = [
                "Decrypt and verify the selected file.",
                f"Source: {details['source_path']}",
                f"Source size: {details['source_length']} bytes",
                f"Source SHA-256: {details['source_sha256']}",
                f"New output: {details['output_path']} ({details['output_selection']})",
                f"Limits: {details['limits']}",
                f"Expected signers: {details['expected_signers']}",
                f"Recipient keys: {details['expected_recipients']}",
                f"Local trust pins: {details['local_trust']}",
                f"Authorization: {details['authorized']}",
            ]
            for name, label in (("owner_pin", "Owner"), ("publisher_pin", "Publisher")):
                if details[name] != "none":
                    lines.append(f"{label}: {details[name]}")
        elif operation.purpose == "seal-envelope":
            lines = [f"Sign and encrypt {details['payload_length']} bytes.",
                     f"Signing key: {details['signer']}",
                     f"Recipients: {details['recipients']}"]
        elif operation.purpose == "open-envelope":
            lines = [f"Decrypt and verify {details['serialized_length']} bytes.",
                     f"Expected signers: {details['expected_signers']}",
                     f"Recipient keys: {details['expected_recipients']}"]
            for name, label in (("owner_pin", "Owner"), ("publisher_pin", "Publisher")):
                if details[name] != "none":
                    lines.append(f"{label}: {details[name]}")
        elif operation.purpose == "enroll-key":
            lines = [f"Enroll this certificate as {details['roles']} in local trust.",
                     f"Fingerprint: {details['fingerprint']}",
                     f"Key profile: {details['profile']}",
                     "This returns a local trust selection; it does not update a stored authority."]
        else:
            lines = [f"Generate a protected {details['profile']} key for this identity.",
                     f"Name: {details['identity_name']}"]
            if details["identity_email"] != "none":
                lines.append(f"Email: {details['identity_email']}")
            if details["identity_comment"] != "none":
                lines.append(f"Comment: {details['identity_comment']}")
            lines.append(f"Certification notation names: {details['notation_names']}")
            lines.append(f"Certification notations: {details['notation_count']}")
            lines.append("The key will be created in the selected local key store.")
        if operation._home_identity is not None:
            lines.append(f"Key store: {operation._home_identity[0]}")
        elif operation.purpose == "generate-key":
            lines.append(f"Key store: {details['keyring_home']}")
        lines.append(f"Operation: {self.operation_id}")
        return "\n".join(lines)

    def _close_and_wipe(self) -> None:
        with self._lock:
            if self._handoff is not None:
                _wipe(self._handoff)
                self._handoff = None
            self._status = "closed"


def _operation(
    purpose: Literal["seal-envelope", "open-envelope", "enroll-key", "generate-key"],
    bindings: Mapping[str, Any],
    summary: str,
    details: tuple[tuple[str, str], ...],
    home_identity: tuple[str, int, int] | None,
    *,
    generation_selection: object | None = None,
) -> InputOperation:
    try:
        canonical = json.dumps(
            {"schema_version": _OPERATION_VERSION, "purpose": purpose, "bindings": bindings},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None
    digest = hashlib.sha256(canonical).hexdigest()
    return InputOperation(
        purpose=purpose,
        digest=digest,
        summary=summary,
        details=details,
        _home_identity=home_identity,
        _generation_selection=generation_selection,
        _seal=_INPUT_OPERATION_SEAL,
    )


def _generation_selection_binding(selection: object) -> dict[str, Any]:
    """Return every inspected generation-home identity in canonical JSON shape."""
    try:
        ancestors = tuple(selection.ancestors)
        initial_leaf = selection.initial_leaf
        return {
            "path": os.fspath(selection.path),
            "ancestors": [_directory_identity_binding(item) for item in ancestors],
            "initial_leaf": None if initial_leaf is None else _directory_identity_binding(initial_leaf),
        }
    except Exception:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _directory_identity_binding(identity: object) -> dict[str, Any]:
    try:
        return {
            "path": os.fspath(identity.path),
            "device": identity.device,
            "inode": identity.inode,
            "uid": identity.uid,
            "mode": identity.mode,
        }
    except Exception:
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _fingerprint(value: str) -> str:
    try:
        return normalize_fingerprint(value)
    except (OpenPGPContractError, TypeError, ValueError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _fingerprint_set(values: Iterable[str], *, nonempty: bool = False) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    try:
        selected = tuple(_fingerprint(value) for value in values)
    except (TypeError, ValueError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None
    if len(selected) != len(set(selected)) or (nonempty and not selected):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    return frozenset(selected)


def _trust_set(local_trust: LocalTrust) -> frozenset[str]:
    if not isinstance(local_trust, LocalTrust):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    return _fingerprint_set(local_trust.fingerprints)


def _capability_set(values: Iterable[KeyCapability | str]) -> frozenset[KeyCapability]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    try:
        selected = frozenset(KeyCapability(value) for value in values)
    except (TypeError, ValueError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None
    if not selected or not selected.issubset({KeyCapability.SIGN, KeyCapability.ENCRYPT}):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION)
    return selected


def _record_binding(record: KeyRecord) -> dict[str, Any]:
    return {
        "fingerprint": _fingerprint(record.fingerprint),
        "algorithm": record.algorithm,
        "bits": record.bits,
        "capabilities": sorted(capability.value for capability in record.capabilities),
        "direct_capabilities": sorted(capability.value for capability in record.direct_capabilities),
        "aggregate_capabilities": sorted(capability.value for capability in record.aggregate_capabilities),
        "created_on": record.created_on.isoformat(),
        "expires_on": None if record.expires_on is None else record.expires_on.isoformat(),
        "expires_epoch": record.expires_epoch,
        "is_primary": record.is_primary,
        "revoked": record.revoked,
        "expired": record.expired,
        "disabled": record.disabled,
    }


def _verified_home_identity(value: str | os.PathLike[str]) -> tuple[str, int, int]:
    try:
        selected = Path(value)
        if not selected.is_absolute() or selected.is_symlink():
            raise ValueError
        home = selected.resolve(strict=True)
        info = home.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_uid != os.geteuid()
        ):
            raise ValueError
        return os.fspath(home), info.st_dev, info.st_ino
    except (OSError, TypeError, ValueError, RuntimeError):
        raise NativeInputError(NativeInputErrorCode.INVALID_OPERATION) from None


def _home_display_id(identity: tuple[str, int, int]) -> str:
    value = json.dumps(
        {"path": identity[0], "device": identity[1], "inode": identity[2]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _display_set(values: Iterable[str]) -> str:
    selected = tuple(sorted(values))
    visible = selected[:5]
    text = ", ".join(visible) if visible else "none"
    if len(selected) > len(visible):
        text += f", and {len(selected) - len(visible)} more"
    return text


def _short_path(value: str) -> str:
    """Keep the compact operation summary within its bounded display size."""
    return value if len(value) <= 240 else "…" + value[-239:]


def _display_path(value: str) -> str:
    """Quote path text so control characters cannot alter native form layout."""
    return json.dumps(value, ensure_ascii=True)


def _limits_display(limits: Mapping[str, int | float]) -> str:
    """Show every bound alongside the native approval prompt."""
    return (
        f"plaintext ≤{limits['max_plaintext_bytes']:,} B; "
        f"envelope ≤{limits['max_envelope_bytes']:,} B; "
        f"temporary ≤{limits['max_temporary_bytes']:,} B; "
        f"diagnostics ≤{limits['max_diagnostics_bytes']:,} B; "
        f"timeout ≤{limits['timeout_seconds']:g} s"
    )


def _valid_passphrase(value: bytearray) -> bool:
    if not 1 <= len(value) <= _MAX_PASSPHRASE_BYTES or any(
        marker in value for marker in (b"\x00", b"\r", b"\n")
    ):
        return False
    return _is_well_formed_utf8(value)


def _is_well_formed_utf8(value: bytearray) -> bool:
    """Validate UTF-8 bytes without constructing an immutable secret string."""
    index = 0
    size = len(value)
    while index < size:
        first = value[index]
        if first <= 0x7F:
            index += 1
            continue
        if 0xC2 <= first <= 0xDF:
            if index + 1 >= size or not 0x80 <= value[index + 1] <= 0xBF:
                return False
            index += 2
            continue
        if 0xE0 <= first <= 0xEF:
            if index + 2 >= size:
                return False
            second, third = value[index + 1], value[index + 2]
            if not 0x80 <= third <= 0xBF:
                return False
            if first == 0xE0:
                valid_second = 0xA0 <= second <= 0xBF
            elif first == 0xED:
                valid_second = 0x80 <= second <= 0x9F
            else:
                valid_second = 0x80 <= second <= 0xBF
            if not valid_second:
                return False
            index += 3
            continue
        if 0xF0 <= first <= 0xF4:
            if index + 3 >= size:
                return False
            second, third, fourth = value[index + 1:index + 4]
            if not (0x80 <= third <= 0xBF and 0x80 <= fourth <= 0xBF):
                return False
            if first == 0xF0:
                valid_second = 0x90 <= second <= 0xBF
            elif first == 0xF4:
                valid_second = 0x80 <= second <= 0x8F
            else:
                valid_second = 0x80 <= second <= 0xBF
            if not valid_second:
                return False
            index += 4
            continue
        return False
    return True


def _is_operation(value: object) -> bool:
    return isinstance(value, InputOperation) and value._seal is _INPUT_OPERATION_SEAL


def _wipe(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)
    value.clear()

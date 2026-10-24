"""Stable machine binding and private-key proof for secure-container setup.

The binding is an equality guard for a local mapping, not an enrollment or
attestation credential. Raw machine identifiers are never returned or stored.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid
from xml.etree import ElementTree

from .contracts import EnvelopePolicy, LocalTrust, normalize_fingerprint
from .envelope import seal_envelope
from .generation import KeyIdentity
from .opening import open_envelope
from .secrets import SecretReference, SecretResolver


_HEX32 = re.compile(r"[0-9a-fA-F]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-f0-9]{12}\Z")
_IOREG = ("/usr/sbin/ioreg", "-a", "-r", "-d", "1", "-c", "IOPlatformExpertDevice")
_APPLE_PLIST_DOCTYPE = (b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                         b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">')
_RIGHTS = ("Private asset. Property of Crux Experts LLC. Use, reproduction, "
           "modification, distribution, or publication without prior written "
           "permission is prohibited.")


class IdentityError(RuntimeError):
    """Safe local identity failure without machine ID, secret, or GnuPG output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MachineBinding:
    provider: str
    digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.provider, str)
                or self.provider not in {"linux-machine-id-v1", "apple-platform-uuid-v1"}):
            raise IdentityError("binding_invalid")
        if not isinstance(self.digest, str) or _HEX64.fullmatch(self.digest) is None:
            raise IdentityError("binding_invalid")


def _digest(provider: str, raw: bytes) -> MachineBinding:
    value = hashlib.sha256(b"LocalSetup secure-container machine binding v1\0"
                           + provider.encode("ascii") + b"\0" + raw).hexdigest()
    return MachineBinding(provider, value)


def _linux_binding(path: Path) -> MachineBinding:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
                raise IdentityError("binding_unavailable")
            raw = os.read(fd, 129)
            after = os.fstat(fd)
            if len(raw) > 128 or (info.st_dev, info.st_ino, info.st_size) != (
                    after.st_dev, after.st_ino, after.st_size):
                raise IdentityError("binding_unavailable")
        finally:
            os.close(fd)
        token = raw.strip(b"\r\n")
        if (_HEX32.fullmatch(token.decode("ascii")) is None
                or token == b"0" * 32 or raw not in {token, token + b"\n"}):
            raise IdentityError("binding_unavailable")
        return _digest("linux-machine-id-v1", bytes.fromhex(token.decode("ascii")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise IdentityError("binding_unavailable") from None


def _ioreg_output() -> bytes:
    """Read only the fixed Apple provider with a wall deadline and stream caps."""
    try:
        process = subprocess.Popen(
            _IOREG, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except OSError:
        raise IdentityError("binding_unavailable") from None
    output = bytearray()
    errors = bytearray()
    deadline = time.monotonic() + 5.0
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, (output, 256 * 1024))
            selector.register(process.stderr, selectors.EVENT_READ, (errors, 8 * 1024))
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IdentityError("binding_unavailable")
                for key, _mask in selector.select(remaining):
                    target, maximum = key.data
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(target) + len(chunk) > maximum:
                        raise IdentityError("binding_unavailable")
                    else:
                        target.extend(chunk)
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise IdentityError("binding_unavailable")
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        raise IdentityError("binding_unavailable") from None
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _apple_binding(payload: bytes) -> MachineBinding:
    if not isinstance(payload, bytes) or not payload or len(payload) > 256 * 1024:
        raise IdentityError("binding_unavailable")
    if _APPLE_PLIST_DOCTYPE in payload:
        prefix, suffix = payload.split(_APPLE_PLIST_DOCTYPE, 1)
        if (prefix.strip() not in (b"", b'<?xml version="1.0" encoding="UTF-8"?>')
                or not suffix.lstrip().startswith(b"<plist")):
            raise IdentityError("binding_unavailable")
        payload = prefix + suffix
    if b"<!" in payload:
        raise IdentityError("binding_unavailable")
    try:
        root = ElementTree.fromstring(payload)
        if root.tag != "plist" or len(root) != 1 or root[0].tag != "array" or len(root[0]) != 1:
            raise ValueError
        device = root[0][0]
        if device.tag != "dict":
            raise ValueError
        nodes = 0
        selected: list[str] = []
        stack = [(root, 0)]
        while stack:
            element, depth = stack.pop()
            nodes += 1
            if nodes > 4096 or depth > 16:
                raise ValueError
            if element.tag == "dict":
                if len(element) % 2:
                    raise ValueError
                seen: set[str] = set()
                for index in range(0, len(element), 2):
                    key, value = element[index], element[index + 1]
                    if key.tag != "key" or key.text is None or key.text in seen:
                        raise ValueError
                    seen.add(key.text)
                    if key.text == "IOPlatformUUID":
                        if element is not device or value.tag != "string" or value.text is None:
                            raise ValueError
                        selected.append(value.text)
            stack.extend((child, depth + 1) for child in element)
        if len(selected) != 1:
            raise ValueError
        parsed = uuid.UUID(selected[0])
        if parsed.int == 0 or selected[0].lower() != str(parsed):
            raise ValueError
        return _digest("apple-platform-uuid-v1", parsed.bytes)
    except (ElementTree.ParseError, ValueError, TypeError):
        raise IdentityError("binding_unavailable") from None


def machine_binding() -> MachineBinding:
    """Observe the stable OS identity without creating keys or local state."""
    if sys.platform.startswith("linux"):
        return _linux_binding(Path("/etc/machine-id"))
    if sys.platform == "darwin":
        return _apple_binding(_ioreg_output())
    raise IdentityError("binding_unavailable")


def default_key_identity(label: str, role: str, year: int) -> KeyIdentity:
    """Build the persisted public recipe; callers record it before generation."""
    if (_LABEL.fullmatch(label) is None or role not in {"Publisher", "Owner"}
            or type(year) is not int or not 2000 <= year <= 9999):
        raise IdentityError("identity_recipe_invalid")
    description = ("https://github.com/CruxExperts/LocalSetup; "
                   f"Copyright © {year} Crux Experts LLC; "
                   "https://www.cruxexperts.com/; " + _RIGHTS)
    return KeyIdentity(
        name=f"LocalSetup Encryption {label} {role}",
        email="pgp@cruxexperts.com",
        certification_notations=(("description@localsetup.cruxexperts.com", description),),
    )


def private_possession_challenge(
    *, home: Path, fingerprint: str, reference: SecretReference,
    resolver: SecretResolver,
) -> None:
    """Prove both signing and decryption without exposing the secret key."""
    try:
        selected = normalize_fingerprint(fingerprint)
        challenge = os.urandom(32)
        trust = LocalTrust(frozenset({selected}))
        sealed = seal_envelope(
            challenge, gnupg_home=home, signer_fingerprint=selected,
            recipient_fingerprints=(selected,), local_trust=trust,
            passphrase_reference=reference, secret_resolver=resolver,
        )
        opened = open_envelope(
            sealed, gnupg_home=home,
            policy=EnvelopePolicy(expected_signers={selected}, expected_recipients={selected}),
            local_trust=trust, passphrase_reference=reference, secret_resolver=resolver,
        )
        if opened != challenge:
            raise IdentityError("private_proof_failed")
    except IdentityError:
        raise
    except Exception:
        raise IdentityError("private_proof_failed") from None

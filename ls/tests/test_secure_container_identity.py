"""Stable, non-disclosing machine binding and signed identity recipe checks."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib

import pytest

from ls.core.openpgp import container_identity as identity
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver


def test_linux_binding_is_stable_and_rejects_unsafe_sources(tmp_path: Path) -> None:
    source = tmp_path / "machine-id"
    source.write_text("1234567890abcdef1234567890abcdef\n")
    first = identity._linux_binding(source)
    assert first.provider == "linux-machine-id-v1"
    assert first.digest == hashlib.sha256(
        b"LocalSetup secure-container machine binding v1\0linux-machine-id-v1\0"
        + bytes.fromhex("1234567890abcdef1234567890abcdef")
    ).hexdigest()
    assert first == identity._linux_binding(source)
    source.write_text("0" * 32 + "\n")
    with pytest.raises(identity.IdentityError, match="binding_unavailable"):
        identity._linux_binding(source)
    source.write_text("1234567890abcdef1234567890abcdef trailing\n")
    with pytest.raises(identity.IdentityError, match="binding_unavailable"):
        identity._linux_binding(source)
    source.unlink()
    source.symlink_to(tmp_path / "other")
    with pytest.raises(identity.IdentityError, match="binding_unavailable"):
        identity._linux_binding(source)


def _apple_plist(uuid: str, *, extra: bytes = b"") -> bytes:
    return (b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><array><dict>'
            b'<key>IOPlatformUUID</key><string>' + uuid.encode("ascii") + b"</string>"
            + extra + b"</dict></array></plist>")


def test_apple_binding_accepts_one_canonical_uuid_only() -> None:
    value = "A75A152F-2314-4AC7-9E99-333CB661D661"
    selected = identity._apple_binding(_apple_plist(value))
    assert selected.provider == "apple-platform-uuid-v1"
    assert len(selected.digest) == 64
    standard = plistlib.dumps([{"IOPlatformUUID": value}], fmt=plistlib.FMT_XML)
    assert identity._APPLE_PLIST_DOCTYPE in standard
    assert identity._apple_binding(standard) == selected
    for payload in (
        _apple_plist(value, extra=b"<key>IOPlatformUUID</key><string>" + value.encode() + b"</string>"),
        _apple_plist("00000000-0000-0000-0000-000000000000"),
        _apple_plist(value.replace("-", "")),
        _apple_plist(value).replace(b"<plist", b"<!DOCTYPE plist><plist"),
        standard.replace(identity._APPLE_PLIST_DOCTYPE,
                         identity._APPLE_PLIST_DOCTYPE + b"<!ENTITY probe 'unsafe'>"),
        standard.replace(identity._APPLE_PLIST_DOCTYPE,
                         identity._APPLE_PLIST_DOCTYPE * 2),
        b"<plist><array><dict></dict></array></plist>",
    ):
        with pytest.raises(identity.IdentityError, match="binding_unavailable"):
            identity._apple_binding(payload)


def test_apple_provider_uses_fixed_argv_and_no_inherited_environment(monkeypatch) -> None:
    payload = _apple_plist("A75A152F-2314-4AC7-9E99-333CB661D661")
    called = []

    class FixtureProcess:
        def __init__(self, argv, **kwargs):
            called.append((argv, kwargs))
            output_read, output_write = os.pipe()
            error_read, error_write = os.pipe()
            os.write(output_write, payload)
            os.close(output_write)
            os.close(error_write)
            self.stdout = os.fdopen(output_read, "rb", buffering=0)
            self.stderr = os.fdopen(error_read, "rb", buffering=0)

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

        def kill(self):
            raise AssertionError("successful provider must not be killed")

    monkeypatch.setattr(identity.subprocess, "Popen", FixtureProcess)
    assert identity._apple_binding(identity._ioreg_output()).provider == "apple-platform-uuid-v1"
    assert len(called) == 1
    argv, options = called[0]
    assert argv == identity._IOREG
    assert options["shell"] is False and options["stdin"] == identity.subprocess.DEVNULL
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


def test_default_recipe_keeps_complete_rights_in_one_signed_description() -> None:
    result = identity.default_key_identity("a1b2c3d4e5f6", "Publisher", 2026)
    assert result.name == "LocalSetup Encryption a1b2c3d4e5f6 Publisher"
    assert result.email == "pgp@cruxexperts.com"
    assert len(result.certification_notations) == 1
    name, description = result.certification_notations[0]
    assert name == "description@localsetup.cruxexperts.com"
    for required in ("https://github.com/CruxExperts/LocalSetup",
                     "Copyright © 2026 Crux Experts LLC", "https://www.cruxexperts.com/",
                     "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, "
                     "distribution, or publication without prior written permission is prohibited."):
        assert required in description


def test_private_challenge_requires_matching_decrypted_nonce(monkeypatch, tmp_path: Path) -> None:
    chosen = "A" * 40
    reference = SecretReference(SecretProvider.ENV, "FIXTURE_KEY")
    resolver = SecretResolver(env_values={"FIXTURE_KEY": "fixture"})
    observed: list[tuple[str, object]] = []

    def seal(payload: bytes, **kwargs):
        observed.append(("seal", kwargs))
        assert len(payload) == 32
        return b"sealed"

    def open_wrong(_payload: bytes, **kwargs):
        observed.append(("open", kwargs))
        return b"wrong"

    monkeypatch.setattr(identity, "seal_envelope", seal)
    monkeypatch.setattr(identity, "open_envelope", open_wrong)
    with pytest.raises(identity.IdentityError, match="private_proof_failed"):
        identity.private_possession_challenge(home=tmp_path, fingerprint=chosen,
                                               reference=reference, resolver=resolver)
    assert [event for event, _ in observed] == ["seal", "open"]
    assert observed[0][1]["recipient_fingerprints"] == (chosen,)
    assert observed[1][1]["policy"].expected_signers == {chosen}

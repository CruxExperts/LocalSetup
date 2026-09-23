from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

from ls.core.openpgp import opening as opening_api
from ls.core.openpgp.contracts import EnvelopeHeader, EnvelopePolicy, LocalTrust
from ls.core.openpgp.opening import (
    EnvelopeOpenError,
    EnvelopeOpenErrorCode,
    open_envelope,
)


SIGNER = "A" * 40
OTHER_SIGNER = "D" * 40
RECIPIENT = "B" * 40
OTHER_RECIPIENT = "C" * 40
_PRIVATE_MARKER = "source-name-and-provenance-must-not-escape"


def _policy(
    *, signer: str = SIGNER, recipients: tuple[str, ...] = (RECIPIENT,)
) -> EnvelopePolicy:
    return EnvelopePolicy(
        expected_signers=(signer,), expected_recipients=recipients
    )


def _outer_envelope(header: str | None = None) -> bytes:
    header_bytes = (header or EnvelopeHeader().to_json()).encode("utf-8")
    return struct.pack(">H", len(header_bytes)) + header_bytes + b"opaque-ciphertext"


def _inner_message(
    payload: bytes,
    *,
    signer: str = SIGNER,
    recipients: tuple[str, ...] = (RECIPIENT,),
    payload_sha256: str | None = None,
) -> bytes:
    manifest = json.dumps(
        {
            "header": EnvelopeHeader().to_json(),
            "payload_length": len(payload),
            "payload_sha256": payload_sha256 or hashlib.sha256(payload).hexdigest(),
            "recipient_fingerprints": list(sorted(recipients)),
            "signer_fingerprint": signer,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return (
        b"localsetup.openpgp-inner\x00\x01"
        + struct.pack(">I", len(manifest))
        + manifest
        + payload
    )


def _status(
    *,
    signers: tuple[str, ...] = (SIGNER,),
    include_signature: bool = True,
    integrity: str = "mdc",
    good_mdc: bool | None = None,
    bad_signature: bool = False,
) -> bytes:
    mdc_method = 2 if integrity == "mdc" else 0
    aead_algorithm = 1 if integrity == "aead" else 0
    lines = [
        f"[GNUPG:] DECRYPTION_INFO {mdc_method} 9 {aead_algorithm} 0"
    ]
    if good_mdc is True:
        lines.append("[GNUPG:] GOODMDC")
    elif good_mdc is False:
        lines.append("[GNUPG:] BADMDC")
    lines.append("[GNUPG:] DECRYPTION_OKAY")
    if bad_signature:
        lines.append(f"[GNUPG:] BADSIG 0123456789ABCDEF {_PRIVATE_MARKER}")
    if include_signature:
        for signer in signers:
            lines.extend(
                (
                    "[GNUPG:] NEWSIG",
                    "[GNUPG:] GOODSIG 0123456789ABCDEF Test Signer",
                    f"[GNUPG:] VALIDSIG {signer} 20260923 1790123456 0 4 0 1 8 00",
                )
            )
    return ("\n".join(lines) + "\n").encode("ascii")


def _fake_gpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plaintext: bytes,
    diagnostics: bytes,
    packet_count: int = 1,
) -> tuple[Path, Path, Path]:
    home = tmp_path / "isolated-gnupg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    executable = tmp_path / "fake-gpg"
    calls = tmp_path / "decrypt-calls"
    packet_listing = (
        ":pubkey enc packet: version 3, algo 1, keyid 0000000000000000\n"
        * packet_count
        + ":encrypted data packet:\n"
    ).encode("ascii")
    script = f"#!{sys.executable}\n" + f"""\
import os
import sys

args = sys.argv[1:]
if "--list-packets" in args:
    sys.stdout.buffer.write({packet_listing!r})
    raise SystemExit(0)
if os.environ.get("GNUPGHOME") != {str(home)!r}:
    raise SystemExit(91)
if "--no-options" not in args or "--no-auto-key-retrieve" not in args:
    raise SystemExit(92)
if "--status-fd=2" not in args or "--decrypt" not in args:
    raise SystemExit(93)
with open({str(calls)!r}, "ab") as stream:
    stream.write(b"decrypt\\n")
sys.stdout.buffer.write({plaintext!r})
sys.stderr.buffer.write({diagnostics!r})
"""
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        opening_api.shutil,
        "which",
        lambda name: str(executable) if name in {"gpg", "gpg2"} else None,
    )
    return home, executable, calls


@pytest.mark.parametrize("integrity", ("mdc", "aead"))
def test_open_returns_exact_payload_after_signature_manifest_and_policy_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    integrity: str,
) -> None:
    payload = b"\x00binary\xff payload\nnot-normalized"
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(payload),
        diagnostics=_status(integrity=integrity),
    )

    opened = open_envelope(
        _outer_envelope(),
        gnupg_home=home,
        policy=_policy(),
        local_trust=LocalTrust({SIGNER}),
    )

    assert opened == payload


def test_open_accepts_real_gpg_three_field_decryption_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00\xffauthenticated"
    diagnostics = _status(integrity="aead").replace(
        b"DECRYPTION_INFO 0 9 1 0", b"DECRYPTION_INFO 0 9 1"
    )
    home, _executable, _calls = _fake_gpg(
        tmp_path, monkeypatch, plaintext=_inner_message(payload), diagnostics=diagnostics
    )
    assert open_envelope(
        _outer_envelope(),
        gnupg_home=home,
        policy=_policy(),
        local_trust=LocalTrust({SIGNER}),
    ) == payload

def test_open_uses_full_primary_fingerprint_from_verified_subkey_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"primary key policy"
    diagnostics = _status().replace(
        f"VALIDSIG {SIGNER} ".encode("ascii"),
        f"VALIDSIG {OTHER_SIGNER} ".encode("ascii"),
    ).replace(b" 8 00\n", f" 8 00 {SIGNER}\n".encode("ascii"))
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(payload),
        diagnostics=diagnostics,
    )

    opened = open_envelope(
        _outer_envelope(),
        gnupg_home=home,
        policy=_policy(),
        local_trust=LocalTrust({SIGNER}),
    )

    assert opened == payload


@pytest.mark.parametrize(
    ("diagnostics", "expected_code"),
    (
        (_status(include_signature=False), EnvelopeOpenErrorCode.SIGNATURE_INVALID),
        (
            _status(signers=(SIGNER, OTHER_SIGNER)),
            EnvelopeOpenErrorCode.SIGNATURE_INVALID,
        ),
        (_status(bad_signature=True), EnvelopeOpenErrorCode.SIGNATURE_INVALID),
        (_status(good_mdc=False), EnvelopeOpenErrorCode.DECRYPTION_FAILED),
        (_status(integrity="none"), EnvelopeOpenErrorCode.DECRYPTION_FAILED),
    ),
)
def test_open_rejects_missing_multiple_or_unprotected_signatures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostics: bytes,
    expected_code: EnvelopeOpenErrorCode,
) -> None:
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"must-not-be-returned"),
        diagnostics=diagnostics,
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER, OTHER_SIGNER}),
        )

    assert raised.value.code is expected_code
    assert str(raised.value) == expected_code.value
    assert "must-not-be-returned" not in str(raised.value)
    assert _PRIVATE_MARKER not in str(raised.value)


def test_open_rejects_untrusted_signer_before_decryption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _executable, calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"private payload"),
        diagnostics=_status(),
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust(),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.UNTRUSTED_SIGNER
    assert not calls.exists()


def test_open_rejects_packet_count_that_disagrees_with_explicit_recipient_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _executable, calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"private payload"),
        diagnostics=_status(),
        packet_count=2,
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER}),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.MALFORMED_CIPHERTEXT
    assert not calls.exists()


def test_open_rejects_crypto_signer_that_differs_from_authenticated_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"private payload", signer=SIGNER),
        diagnostics=_status(signers=(OTHER_SIGNER,)),
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER, OTHER_SIGNER}),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.SIGNER_SET_MISMATCH


def test_open_rejects_authenticated_recipient_set_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"private payload", recipients=(OTHER_RECIPIENT,)),
        diagnostics=_status(),
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER}),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.RECIPIENT_SET_MISMATCH


def test_open_rejects_tampered_inner_payload_without_disclosing_manifest_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _PRIVATE_MARKER.encode("ascii")
    home, _executable, _calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(payload, payload_sha256="0" * 64),
        diagnostics=_status(),
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER}),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.INVALID_INNER_MESSAGE
    assert _PRIVATE_MARKER not in str(raised.value)


def test_open_rejects_unsupported_public_schema_before_gpg_or_plaintext_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _executable, calls = _fake_gpg(
        tmp_path,
        monkeypatch,
        plaintext=_inner_message(b"private payload"),
        diagnostics=_status(),
    )
    unsupported_header = json.dumps(
        {
            "format": "localsetup.openpgp-envelope",
            "schema_version": 2,
            "filename": _PRIVATE_MARKER,
        },
        separators=(",", ":"),
    )

    with pytest.raises(EnvelopeOpenError) as raised:
        open_envelope(
            _outer_envelope(unsupported_header),
            gnupg_home=home,
            policy=_policy(),
            local_trust=LocalTrust({SIGNER}),
        )

    assert raised.value.code is EnvelopeOpenErrorCode.INVALID_ENVELOPE
    assert _PRIVATE_MARKER not in str(raised.value)
    assert not calls.exists()

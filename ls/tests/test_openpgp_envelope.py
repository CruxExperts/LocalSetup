from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from ls.core.openpgp import ContractErrorCode, LocalTrust, OpenPGPContractError
from ls.core.openpgp import envelope as envelope_api
from ls.core.openpgp.contracts import EnvelopeHeader
from ls.core.openpgp.envelope import (
    EnvelopeError,
    EnvelopeErrorCode,
    parse_envelope,
    parse_inner_message,
    seal_envelope,
)
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver


SIGNER = "A" * 40
RECIPIENT_ONE = "B" * 40
RECIPIENT_TWO = "C" * 40
TEST_SECRET_NAME = "LS_OPENPGP_ENVELOPE_TEST_SECRET"
TEST_SECRET_VALUE = "synthetic-only-passphrase"


def _fake_gpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packet_listing: str,
) -> tuple[Path, Path, Path]:
    captured_inner = tmp_path / "captured-inner.bin"
    captured_arguments = tmp_path / "gpg-arguments.jsonl"
    executable = tmp_path / "fake-gpg"
    script = f"#!{sys.executable}\n" + f"""\
import json
import os
import sys

args = sys.argv[1:]
with open({str(captured_arguments)!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if "--list-packets" in args:
    sys.stdout.write({packet_listing!r})
    raise SystemExit(0)
if "{TEST_SECRET_NAME}" in os.environ:
    raise SystemExit(11)
if "--passphrase-fd" not in args:
    raise SystemExit(12)
passphrase_fd = int(args[args.index("--passphrase-fd") + 1])
if os.read(passphrase_fd, 8192) != b"{TEST_SECRET_VALUE}\\n":
    raise SystemExit(13)
if any({TEST_SECRET_VALUE!r} == arg for arg in args):
    raise SystemExit(14)
inner = sys.stdin.buffer.read()
if not inner.startswith(b"localsetup.openpgp-inner\\x00\\x01"):
    raise SystemExit(15)
with open({str(captured_inner)!r}, "wb") as stream:
    stream.write(inner)
sys.stdout.buffer.write(b"synthetic-ciphertext")
"""
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        envelope_api.shutil,
        "which",
        lambda name: str(executable) if name == "gpg" else None,
    )
    return executable, captured_inner, captured_arguments


def _private_home(tmp_path: Path) -> Path:
    home = tmp_path / "explicit-gnupg-home"
    home.mkdir(mode=0o700)
    return home


def _seal(
    home: Path,
    payload: bytes,
) -> bytes:
    return seal_envelope(
        payload,
        gnupg_home=home,
        signer_fingerprint=SIGNER,
        recipient_fingerprints=(RECIPIENT_ONE, RECIPIENT_TWO),
        local_trust=LocalTrust(frozenset({SIGNER})),
        passphrase_reference=SecretReference(SecretProvider.ENV, TEST_SECRET_NAME),
        secret_resolver=SecretResolver(),
    )


def _hidden_packets(count: int) -> str:
    packets = ":pubkey enc packet: version 3, algo 1, keyid 0000000000000000\n"
    return packets * count + ":encrypted data packet:\n"


def test_seal_preserves_binary_payload_and_uses_only_pinned_hidden_recipients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_NAME, TEST_SECRET_VALUE)
    home = _private_home(tmp_path)
    _, captured_inner, captured_arguments = _fake_gpg(
        tmp_path, monkeypatch, _hidden_packets(2)
    )
    payload = b"\x00leading\n\xffmiddle\x00trailing"

    sealed = _seal(home, payload)

    parsed = parse_envelope(sealed)
    assert parsed.header == EnvelopeHeader()
    assert parsed.ciphertext == b"synthetic-ciphertext"
    inner = parse_inner_message(captured_inner.read_bytes())
    assert inner.payload == payload
    assert inner.manifest.header == parsed.header
    assert inner.manifest.signer_fingerprint == SIGNER
    assert inner.manifest.recipient_fingerprints == (RECIPIENT_ONE, RECIPIENT_TWO)
    assert inner.manifest.payload_length == len(payload)

    sign_arguments, packet_arguments = (
        json.loads(line) for line in captured_arguments.read_text().splitlines()
    )
    recipients = [
        sign_arguments[index + 1]
        for index, argument in enumerate(sign_arguments[:-1])
        if argument == "--recipient"
    ]
    assert recipients == [RECIPIENT_ONE, RECIPIENT_TWO]
    assert sign_arguments[sign_arguments.index("--local-user") + 1] == SIGNER
    assert "--throw-keyids" in sign_arguments
    assert "--no-encrypt-to" in sign_arguments
    assert "--encrypt-to" not in sign_arguments
    assert "--passphrase-fd" in sign_arguments
    assert TEST_SECRET_VALUE not in sign_arguments
    assert sign_arguments[
        sign_arguments.index("--set-filename") + 1
    ] == ""
    assert "--list-only" in packet_arguments
    assert "--list-packets" in packet_arguments

    raw_inner = captured_inner.read_bytes()
    tampered_inner = raw_inner[:-1] + bytes((raw_inner[-1] ^ 1,))
    with pytest.raises(EnvelopeError) as raised:
        parse_inner_message(tampered_inner)
    assert raised.value.code is EnvelopeErrorCode.INVALID_INNER_MESSAGE


def test_seal_accepts_hidden_aead_encrypted_packet_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEST_SECRET_NAME, TEST_SECRET_VALUE)
    home = _private_home(tmp_path)
    aead_listing = _hidden_packets(2).replace(
        ":encrypted data packet:",
        ":aead encrypted packet: cipher=9 aead=2 cb=16",
    )
    _fake_gpg(tmp_path, monkeypatch, aead_listing)

    sealed = _seal(home, b"synthetic payload")

    assert parse_envelope(sealed).ciphertext == b"synthetic-ciphertext"


def test_clear_header_rejects_extra_metadata() -> None:
    header = json.dumps(
        {
            "format": "localsetup.openpgp-envelope",
            "filename": "private-name.txt",
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    serialized = struct.pack(">H", len(header)) + header + b"ciphertext"

    with pytest.raises(EnvelopeError) as raised:
        parse_envelope(serialized)
    assert raised.value.code is EnvelopeErrorCode.INVALID_ENVELOPE


@pytest.mark.parametrize(
    "packet_listing",
    (
        _hidden_packets(1),
        ":pubkey enc packet: version 3, algo 1, keyid 0123456789ABCDEF\n"
        ":pubkey enc packet: version 3, algo 1, keyid 0000000000000000\n"
        ":encrypted data packet:\n",
        _hidden_packets(2).replace(
            ":encrypted data packet:",
            ":symkey enc packet: version 4, cipher 9, s2k 3\n"
            ":encrypted data packet:",
        ),
    ),
)
def test_seal_rejects_mismatched_or_visible_recipient_packet_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packet_listing: str,
) -> None:
    monkeypatch.setenv(TEST_SECRET_NAME, TEST_SECRET_VALUE)
    home = _private_home(tmp_path)
    _fake_gpg(tmp_path, monkeypatch, packet_listing)

    with pytest.raises(EnvelopeError) as raised:
        _seal(home, b"synthetic payload")
    assert raised.value.code is EnvelopeErrorCode.MALFORMED_CIPHERTEXT


def test_signer_must_be_present_in_local_trust_before_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    monkeypatch.delenv(TEST_SECRET_NAME, raising=False)
    monkeypatch.setattr(
        envelope_api.shutil,
        "which",
        lambda _name: pytest.fail("GPG lookup must follow the local trust check"),
    )

    with pytest.raises(OpenPGPContractError) as raised:
        seal_envelope(
            b"payload",
            gnupg_home=home,
            signer_fingerprint=SIGNER,
            recipient_fingerprints=(RECIPIENT_ONE,),
            local_trust=LocalTrust(),
            passphrase_reference=SecretReference(SecretProvider.ENV, TEST_SECRET_NAME),
            secret_resolver=SecretResolver(),
        )
    assert raised.value.code is ContractErrorCode.UNTRUSTED_SIGNER


def test_seal_bounds_untrusted_recipient_iterators_before_crypto(
    tmp_path: Path,
) -> None:
    def oversized_recipients():
        for index in range(65):
            yield f"{index:040X}"
        raise AssertionError("recipient iterator read past the configured bound")

    with pytest.raises(EnvelopeError) as raised:
        seal_envelope(
            b"payload",
            gnupg_home=_private_home(tmp_path),
            signer_fingerprint=SIGNER,
            recipient_fingerprints=oversized_recipients(),
            local_trust=LocalTrust(frozenset({SIGNER})),
            passphrase_reference=SecretReference(
                SecretProvider.ENV, TEST_SECRET_NAME
            ),
            secret_resolver=SecretResolver(),
        )
    assert raised.value.code is EnvelopeErrorCode.INVALID_INPUT

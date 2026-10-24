from __future__ import annotations

import json
import struct
import sys
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from ls.core.openpgp import ContractErrorCode, LocalTrust, OpenPGPContractError
from ls.core.openpgp import envelope as envelope_api
from ls.core.openpgp import recovery_process as recovery_process_api
from ls.core.openpgp.contracts import EnvelopeHeader
from ls.core.openpgp.envelope import (
    EnvelopeError,
    EnvelopeErrorCode,
    parse_envelope,
    parse_inner_message,
    seal_envelope,
)
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from ls.core.openpgp.native_input import describe_seal_operation


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
    monkeypatch.setattr(
        envelope_api, "_managed_gpg_agent",
        lambda _home, before_launch=None: nullcontext(),
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


def test_managed_agent_rejects_relative_home_before_agent_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recovery_process_api,
        "_home_agent_is_running",
        lambda _home: pytest.fail("invalid homes must not reach agent probing"),
    )

    with pytest.raises(EnvelopeError) as raised:
        with envelope_api._managed_gpg_agent("relative-keyring-home"):
            pytest.fail("invalid homes must not enter the agent context")

    assert raised.value.code is EnvelopeErrorCode.INVALID_GPG_HOME


def test_managed_agent_preserves_existing_selected_home_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = _private_home(tmp_path)
    monkeypatch.setattr(
        recovery_process_api,
        "_home_agent_is_running",
        lambda selected_home: selected_home == home,
    )
    monkeypatch.setattr(
        recovery_process_api,
        "_stop_home_agent",
        lambda _home: pytest.fail("an existing agent must not be stopped"),
    )
    monkeypatch.setattr(
        envelope_api.shutil,
        "which",
        lambda _name: pytest.fail("an existing agent must not be relaunched"),
    )

    with envelope_api._managed_gpg_agent(home) as selected:
        assert selected == home


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


@pytest.mark.parametrize("failure", ["operation_mismatch", "owner_loss_after_handoff"])
def test_native_seal_checks_operation_and_owner_before_gpg_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    home = _private_home(tmp_path)
    _, _captured_inner, captured_arguments = _fake_gpg(
        tmp_path, monkeypatch, _hidden_packets(2),
    )
    trust = LocalTrust({SIGNER})
    recipients = (RECIPIENT_ONE, RECIPIENT_TWO)
    payload = b"exact native-input operation"
    expected = describe_seal_operation(
        payload=payload if failure == "owner_loss_after_handoff" else b"different payload",
        signer_fingerprint=SIGNER, recipient_fingerprints=recipients,
        local_trust=trust, gnupg_home=home,
    )

    class FakeNativeInput:
        def __init__(self):
            self.expected = expected
            self.checks = 0
            self.requests = 0
            self.finished = False
            self.buffer = None
            self.lease = None

        def claim(self, operation):
            if operation != self.expected:
                raise RuntimeError("operation mismatch")
            self.lease = object()
            return self.lease

        def request_passphrase(self, operation):
            if operation != self.expected:
                raise RuntimeError("operation mismatch")
            self.requests += 1
            self.buffer = bytearray(TEST_SECRET_VALUE.encode("utf-8"))
            return self.buffer

        def checkpoint(self, operation):
            self.checks += 1
            if operation != self.expected:
                raise RuntimeError("operation mismatch")
            if failure == "owner_loss_after_handoff":
                raise RuntimeError("owner no longer current")

        def finish(self, lease):
            assert lease is self.lease
            self.finished = True

    native_input = FakeNativeInput()
    monkeypatch.setattr(envelope_api, "NativeInputContext", FakeNativeInput)
    resolver = SecretResolver()
    monkeypatch.setattr(
        resolver, "resolve_buffer",
        lambda reference, *, operation, native_input: native_input.request_passphrase(operation),
    )
    if failure == "owner_loss_after_handoff":
        @contextmanager
        def managed_agent(selected_home, *, before_launch=None):
            if before_launch is not None:
                before_launch()
            yield selected_home

        monkeypatch.setattr(envelope_api, "_managed_gpg_agent", managed_agent)

    with pytest.raises(EnvelopeError) as raised:
        seal_envelope(
            payload, gnupg_home=home, signer_fingerprint=SIGNER,
            recipient_fingerprints=recipients, local_trust=trust,
            passphrase_reference=None, secret_resolver=resolver,
            native_input=native_input,
        )

    assert native_input.finished is (failure == "owner_loss_after_handoff")
    assert not captured_arguments.exists(), "GnuPG ran before the native-input check passed"
    assert native_input.requests == (0 if failure == "operation_mismatch" else 1)
    assert native_input.checks == (0 if failure == "operation_mismatch" else 1)
    if native_input.buffer is not None:
        assert not any(native_input.buffer)
    assert raised.value.code is EnvelopeErrorCode.CANCELLED


def test_native_seal_rechecks_owner_before_gpgconf_agent_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    real_managed_agent = envelope_api._managed_gpg_agent
    _, _captured_inner, captured_arguments = _fake_gpg(
        tmp_path, monkeypatch, _hidden_packets(2),
    )
    monkeypatch.setattr(envelope_api, "_managed_gpg_agent", real_managed_agent)
    trust = LocalTrust({SIGNER})
    recipients = (RECIPIENT_ONE, RECIPIENT_TWO)
    payload = b"owner check before agent startup"
    operation = describe_seal_operation(
        payload=payload, signer_fingerprint=SIGNER,
        recipient_fingerprints=recipients, local_trust=trust, gnupg_home=home,
    )
    owner_live = True

    class FakeNativeInput:
        def __init__(self):
            self.buffer = bytearray(TEST_SECRET_VALUE.encode("utf-8"))
            self.finished = False
            self.lease = None

        def claim(self, actual):
            assert actual == operation
            self.lease = object()
            return self.lease

        def request_passphrase(self, actual):
            assert actual == operation
            return self.buffer

        def checkpoint(self, actual):
            assert actual == operation
            if not owner_live:
                raise RuntimeError("owner no longer current")

        def finish(self, lease):
            assert lease is self.lease
            self.finished = True

    native_input = FakeNativeInput()
    monkeypatch.setattr(envelope_api, "NativeInputContext", FakeNativeInput)
    resolver = SecretResolver()
    monkeypatch.setattr(
        resolver, "resolve_buffer",
        lambda reference, *, operation, native_input: native_input.request_passphrase(operation),
    )
    monkeypatch.setattr(
        recovery_process_api, "_managed_home_agents", lambda _homes: nullcontext(),
    )

    def agent_running(_home):
        nonlocal owner_live
        owner_live = False
        return False

    monkeypatch.setattr(recovery_process_api, "_home_agent_is_running", agent_running)
    original_which = envelope_api.shutil.which
    monkeypatch.setattr(
        envelope_api.shutil, "which",
        lambda name: "/synthetic/gpgconf" if name == "gpgconf" else original_which(name),
    )
    launches: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        envelope_api.subprocess, "run",
        lambda command, **_kwargs: launches.append(tuple(command)),
    )

    with pytest.raises(EnvelopeError) as raised:
        seal_envelope(
            payload, gnupg_home=home, signer_fingerprint=SIGNER,
            recipient_fingerprints=recipients, local_trust=trust,
            passphrase_reference=None, secret_resolver=resolver,
            native_input=native_input,
        )

    assert raised.value.code is EnvelopeErrorCode.CANCELLED
    assert owner_live is False and native_input.finished
    assert not any(native_input.buffer)
    assert launches == []
    assert set(home.iterdir()) == set()
    assert not captured_arguments.exists()


def test_concurrent_native_seals_leave_winning_consumer_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_home(tmp_path)
    _, _captured_inner, captured_arguments = _fake_gpg(
        tmp_path, monkeypatch, _hidden_packets(2),
    )
    payload = b"single consumer owns this operation"
    trust = LocalTrust({SIGNER})
    recipients = (RECIPIENT_ONE, RECIPIENT_TWO)
    expected = describe_seal_operation(
        payload=payload, signer_fingerprint=SIGNER,
        recipient_fingerprints=recipients, local_trust=trust, gnupg_home=home,
    )
    request_started = threading.Event()
    allow_request_to_finish = threading.Event()

    class FakeNativeInput:
        def __init__(self):
            self.lock = threading.Lock()
            self.lease = None
            self.owner_thread = None
            self.requests = 0
            self.finished = []
            self.buffer = bytearray(TEST_SECRET_VALUE.encode("utf-8"))

        def claim(self, operation):
            assert operation == expected
            with self.lock:
                if self.lease is not None:
                    raise RuntimeError("already claimed")
                self.lease = object()
                self.owner_thread = threading.get_ident()
                return self.lease

        def request_passphrase(self, operation):
            assert operation == expected
            assert threading.get_ident() == self.owner_thread
            self.requests += 1
            request_started.set()
            assert allow_request_to_finish.wait(2)
            return self.buffer

        def checkpoint(self, operation):
            assert operation == expected
            assert threading.get_ident() == self.owner_thread

        def finish(self, lease):
            assert lease is self.lease
            assert threading.get_ident() == self.owner_thread
            with self.lock:
                self.finished.append(lease)
                self.lease = None
                self.owner_thread = None

    native_input = FakeNativeInput()
    monkeypatch.setattr(envelope_api, "NativeInputContext", FakeNativeInput)
    resolver = SecretResolver()
    monkeypatch.setattr(
        resolver, "resolve_buffer",
        lambda reference, *, operation, native_input: native_input.request_passphrase(operation),
    )
    winner_result: list[bytes] = []
    winner_error: list[BaseException] = []

    def run_winner() -> None:
        try:
            winner_result.append(seal_envelope(
                payload, gnupg_home=home, signer_fingerprint=SIGNER,
                recipient_fingerprints=recipients, local_trust=trust,
                passphrase_reference=None, secret_resolver=resolver,
                native_input=native_input,
            ))
        except BaseException as exc:  # captured for assertion in the test thread
            winner_error.append(exc)

    thread = threading.Thread(target=run_winner)
    thread.start()
    try:
        assert request_started.wait(2), "winning consumer did not request native input"
        with pytest.raises(EnvelopeError) as raised:
            seal_envelope(
                payload, gnupg_home=home, signer_fingerprint=SIGNER,
                recipient_fingerprints=recipients, local_trust=trust,
                passphrase_reference=None, secret_resolver=resolver,
                native_input=native_input,
            )
        assert raised.value.code is EnvelopeErrorCode.CANCELLED
        assert native_input.lease is not None
        assert native_input.finished == [], "rejected consumer must not finish the winner's lease"
    finally:
        allow_request_to_finish.set()
        thread.join(5)

    assert not thread.is_alive()
    assert winner_error == []
    assert len(winner_result) == 1
    assert native_input.requests == 1, "only the winning operation may request the passphrase"
    assert len(native_input.finished) == 1
    assert not any(native_input.buffer)
    launches = [json.loads(line) for line in captured_arguments.read_text().splitlines()]
    assert sum("--passphrase-fd" in args for args in launches) == 1


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

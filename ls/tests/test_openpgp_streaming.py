"""Real-key file envelope behavior across independently owned GnuPG homes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
from types import SimpleNamespace

import pytest

from ls.core.openpgp import (
    EnvelopeError, EnvelopeErrorCode, EnvelopeOpenError, EnvelopePolicy, FileEnvelopeLimits,
    LocalTrust, SecretProvider, SecretReference, SecretResolver,
    accept_envelope_file, initialize_trust_state, open_envelope,
    open_envelope_file, open_historical_envelope_file, seal_envelope, seal_envelope_file,
)
from ls.core.openpgp import envelope_io, publishing_transition, transition, trust_state
from ls.core.openpgp.native_input import (
    NativeInputContext, NativeInputError, NativeInputErrorCode,
    describe_seal_file_operation,
)
from ls.tests.openpgp_key_helpers import (
    _create_key, _gpg, _register_gpg_home_cleanup, PASSPHRASE,
)


@pytest.fixture(scope="module")
def homes(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest):
    if not envelope_io._fd_publication_available():
        pytest.skip("file publication requires Linux procfs and linkat support")
    if shutil.which("gpg") is None:
        pytest.skip("GnuPG required for disposable file envelope keys")
    base = tmp_path_factory.mktemp("file-envelope-homes")
    sender = base / "sender"
    receiver = base / "receiver"
    sender.mkdir(mode=0o700)
    receiver.mkdir(mode=0o700)
    _register_gpg_home_cleanup(request, sender)
    _register_gpg_home_cleanup(request, receiver)
    signer, signer_cert = _create_key(sender, "Stream Sender")
    recipient, recipient_cert = _create_key(receiver, "Stream Recipient")
    _gpg(sender, "--import", input_data=recipient_cert)
    _gpg(receiver, "--import", input_data=signer_cert)
    return sender, receiver, signer, recipient


@pytest.fixture
def stream_env(tmp_path: Path, homes):
    sender, receiver, signer, recipient = homes
    source_dir = tmp_path / "source"
    sealed_dir = tmp_path / "sealed"
    released_dir = tmp_path / "released"
    for directory in (source_dir, sealed_dir, released_dir):
        directory.mkdir(mode=0o700)
    return {
        "sender": sender, "receiver": receiver, "signer": signer,
        "recipient": recipient, "source": source_dir / "payload.bin",
        "sealed": sealed_dir / "payload.pgp", "released": released_dir / "payload.bin",
        "limits": FileEnvelopeLimits(
            max_plaintext_bytes=24 * 1024 * 1024,
            max_envelope_bytes=48 * 1024 * 1024,
            max_temporary_bytes=70 * 1024 * 1024,
            max_diagnostics_bytes=256 * 1024,
            timeout_seconds=120,
        ),
        "reference": SecretReference(SecretProvider.ENV, "LS_OPENPGP_STREAM_TEST_SECRET"),
        "resolver": SecretResolver(env_values={"LS_OPENPGP_STREAM_TEST_SECRET": PASSPHRASE}),
        "policy": EnvelopePolicy(
            expected_signers=(signer,), expected_recipients=(recipient,),
        ),
    }


def _seal(env):
    return seal_envelope_file(
        env["source"], env["sealed"], limits=env["limits"],
        gnupg_home=env["sender"], signer_fingerprint=env["signer"],
        recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
    )


def _open(env, *, sealed=None, released=None, policy=None, authorized=True, **kwargs):
    return open_envelope_file(
        sealed or env["sealed"], released or env["released"],
        limits=env["limits"], gnupg_home=env["receiver"],
        policy=policy or env["policy"], local_trust=LocalTrust({env["signer"]}),
        authorized=authorized, passphrase_reference=env["reference"],
        secret_resolver=env["resolver"], **kwargs,
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class _NativeInputFixture(NativeInputContext):
    def __init__(self, expected, *, on_request=None, cancel=False):
        self.expected = expected
        self.on_request = on_request
        self.cancel = cancel
        self.request_count = 0
        self.finished = 0
        self.buffers: list[bytearray] = []
        self.claim_deadline: float | None = None

    def claim(self, operation, *, deadline=None):
        if operation != self.expected:
            raise NativeInputError(NativeInputErrorCode.OPERATION_MISMATCH)
        self.claim_deadline = deadline
        return object()

    def request_passphrase(self, operation):
        self.request_count += 1
        if self.cancel:
            raise NativeInputError(NativeInputErrorCode.INPUT_UNAVAILABLE)
        if self.on_request is not None:
            result = self.on_request()
        else:
            result = bytearray(PASSPHRASE.encode("utf-8"))
        self.buffers.append(result)
        return result

    def checkpoint(self, operation):
        return None

    def finish(self, lease=None):
        self.finished += 1


def test_independent_homes_stream_more_than_legacy_payload_limit(stream_env):
    env = stream_env
    with env["source"].open("wb") as destination:
        for _ in range(17):
            destination.write(os.urandom(1024 * 1024))
    env["source"].chmod(0o600)
    original_digest = _digest(env["source"])
    assert env["source"].stat().st_size > 16 * 1024 * 1024
    assert _seal(env) == env["sealed"]
    assert _open(env) == env["released"]
    assert _digest(env["released"]) == original_digest
    assert env["released"].stat().st_mode & 0o077 == 0
    assert not tuple(env["released"].parent.glob(".ls-openpgp-*"))
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))
    with pytest.raises(EnvelopeError):
        seal_envelope(
            env["source"].read_bytes(), gnupg_home=env["sender"],
            signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}),
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    with pytest.raises(EnvelopeOpenError):
        open_envelope(
            env["sealed"].read_bytes(), gnupg_home=env["receiver"],
            policy=env["policy"], local_trust=LocalTrust({env["signer"]}),
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )


def test_byte_envelope_roundtrip_uses_disposable_homes_and_wipes_resolved_buffers(stream_env):
    env = stream_env
    payload = b"actual GnuPG byte roundtrip\x00\xffwith mutable passphrase buffers"

    class TrackingResolver(SecretResolver):
        buffers: list[bytearray]

        def __init__(self):
            super().__init__(env_values={"LS_OPENPGP_STREAM_TEST_SECRET": PASSPHRASE})
            self.buffers = []

        def resolve_buffer(self, reference, *, operation, native_input=None):
            result = super().resolve_buffer(
                reference, operation=operation, native_input=native_input,
            )
            self.buffers.append(result)
            return result

    resolver = TrackingResolver()
    sealed = seal_envelope(
        payload, gnupg_home=env["sender"], signer_fingerprint=env["signer"],
        recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=resolver,
    )
    assert len(resolver.buffers) == 1
    assert not any(resolver.buffers[0])
    opened = open_envelope(
        sealed, gnupg_home=env["receiver"], policy=env["policy"],
        local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=resolver,
    )
    assert opened == payload
    assert len(resolver.buffers) == 2
    assert all(not any(buffer) for buffer in resolver.buffers)


def test_stream_denies_unsafe_output_tampering_wrong_policy_and_limits(stream_env):
    env = stream_env
    env["source"].write_bytes(b"protected input\x00binary")
    env["source"].chmod(0o600)
    _seal(env)
    with pytest.raises(EnvelopeOpenError):
        _open(env, authorized=False)
    assert not env["released"].exists()
    wrong = EnvelopePolicy(
        expected_signers=(env["signer"],), expected_recipients=("F" * 40,),
    )
    with pytest.raises(EnvelopeOpenError):
        _open(env, policy=wrong)
    assert not env["released"].exists()
    changed = bytearray(env["sealed"].read_bytes())
    changed[-1] ^= 1
    tampered = env["sealed"].with_name("tampered.pgp")
    tampered.write_bytes(changed)
    with pytest.raises(EnvelopeOpenError):
        _open(env, sealed=tampered)
    assert not env["released"].exists()
    assert not tuple(env["released"].parent.glob(".ls-openpgp-*"))
    env["released"].write_bytes(b"existing")
    with pytest.raises(EnvelopeOpenError):
        _open(env)
    assert env["released"].read_bytes() == b"existing"
    env["released"].unlink()
    symlink = env["released"].with_name("symlink.pgp")
    symlink.symlink_to(env["sealed"])
    with pytest.raises(EnvelopeOpenError):
        _open(env, sealed=symlink)
    assert not env["released"].exists()


def test_stream_seal_denies_source_change_and_cleans_owned_temps(stream_env, monkeypatch):
    env = stream_env
    env["source"].write_bytes(b"initial source")
    env["source"].chmod(0o600)
    from ls.core.openpgp import envelope_io

    real_copy = envelope_io.copy_file

    def change_after_copy(*args, **kwargs):
        real_copy(*args, **kwargs)
        env["source"].write_bytes(b"modified source")

    monkeypatch.setattr(envelope_io, "copy_file", change_after_copy)
    with pytest.raises(EnvelopeError):
        _seal(env)
    assert not env["sealed"].exists()
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))


def test_stream_nonregular_source_and_postlink_failure_cleanup(stream_env, monkeypatch):
    env = stream_env
    os.mkfifo(env["source"], mode=0o600)
    with pytest.raises(EnvelopeError):
        _seal(env)
    assert not env["sealed"].exists()
    env["source"].unlink()
    target = env["source"].with_name("actual-source")
    target.write_bytes(b"protected target")
    target.chmod(0o600)
    env["source"].symlink_to(target)
    with pytest.raises(EnvelopeError):
        _seal(env)
    assert not env["sealed"].exists()
    env["source"].unlink()
    env["source"].write_bytes(b"release only after directory sync")
    env["source"].chmod(0o600)
    real_fsync = envelope_io.os.fsync

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected post-link directory sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(envelope_io.os, "fsync", fail_directory_sync)
    with pytest.raises(EnvelopeError):
        _seal(env)
    assert not env["sealed"].exists()
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))


def test_stream_cancel_and_temp_budget_apply_before_publication(stream_env, monkeypatch):
    env = stream_env
    env["source"].write_bytes(b"cancel before seal")
    env["source"].chmod(0o600)
    with pytest.raises(EnvelopeError) as cancelled:
        seal_envelope_file(
            env["source"], env["sealed"], limits=env["limits"],
            gnupg_home=env["sender"], signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}),
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
            cancel_check=lambda: True,
        )
    assert cancelled.value.code is EnvelopeErrorCode.CANCELLED
    assert not env["sealed"].exists()
    tiny = FileEnvelopeLimits(
        max_plaintext_bytes=1024, max_envelope_bytes=2048,
        max_temporary_bytes=4, max_diagnostics_bytes=1024, timeout_seconds=10,
    )
    with pytest.raises(EnvelopeError):
        seal_envelope_file(
            env["source"], env["sealed"], limits=tiny,
            gnupg_home=env["sender"], signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}),
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    assert not env["sealed"].exists()
    _seal(env)
    too_small = FileEnvelopeLimits(
        max_plaintext_bytes=1024, max_envelope_bytes=8,
        max_temporary_bytes=2048, max_diagnostics_bytes=1024,
        timeout_seconds=10,
    )
    with pytest.raises(EnvelopeOpenError):
        open_envelope_file(
            env["sealed"], env["released"], limits=too_small,
            gnupg_home=env["receiver"], policy=env["policy"],
            local_trust=LocalTrust({env["signer"]}), authorized=True,
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    assert not env["released"].exists()
    expired = FileEnvelopeLimits(
        max_plaintext_bytes=1024, max_envelope_bytes=2048,
        max_temporary_bytes=4096, max_diagnostics_bytes=1024,
        timeout_seconds=0.000000001,
    )
    with pytest.raises(EnvelopeOpenError):
        open_envelope_file(
            env["sealed"], env["released"], limits=expired,
            gnupg_home=env["receiver"], policy=env["policy"],
            local_trust=LocalTrust({env["signer"]}), authorized=True,
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    assert not env["released"].exists()
    signal = {"cancel": False}
    real_strip = envelope_io._strip_inner_prefix

    def cancel_after_verified_copy(*args, **kwargs):
        real_strip(*args, **kwargs)
        signal["cancel"] = True

    monkeypatch.setattr(envelope_io, "_strip_inner_prefix", cancel_after_verified_copy)
    with pytest.raises(EnvelopeOpenError):
        _open(env, cancel_check=lambda: signal["cancel"])
    assert not env["released"].exists()
    assert not tuple(env["released"].parent.glob(".ls-openpgp-*"))


def _trust_for_file(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    owner_certificate = _gpg(env["receiver"], "--export", env["recipient"])
    publisher_certificate = _gpg(env["sender"], "--export", env["signer"])
    fingers = {
        owner_certificate: env["recipient"],
        publisher_certificate: env["signer"],
    }
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(
            primary_fingerprint=fingers[certificate]
        ),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)
    private = tmp_path / "trust"
    private.mkdir(mode=0o700)
    path = private / "trust.db"
    snapshot = initialize_trust_state(
        path, scope=("stream:fixture",), owner_certificate=owner_certificate,
        publisher_certificate=publisher_certificate,
    )
    return path, snapshot


def test_file_receipt_current_trust_and_exact_historical_reopen(
    stream_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    env = stream_env
    env["source"].write_bytes(b"accepted original\x00bytes")
    env["source"].chmod(0o600)
    _seal(env)
    path, snapshot = _trust_for_file(env, tmp_path, monkeypatch)
    history_output = env["released"].with_name("history.bin")
    args = dict(
        limits=env["limits"], trust_state_path=path,
        expected_scope=("stream:fixture",), role="publisher",
        gnupg_home=env["receiver"], policy=env["policy"], authorized=True,
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
    )
    with pytest.raises(EnvelopeOpenError):
        open_historical_envelope_file(
            env["sealed"], history_output,
            signer_fingerprint=env["signer"], **args,
        )
    assert not history_output.exists()
    accepted = accept_envelope_file(
        env["sealed"], env["released"], expected_store_id=snapshot.store_id,
        expected_revision=snapshot.revision, **args,
    )
    assert accepted.read_bytes() == b"accepted original\x00bytes"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    trust_state.revoke_authority(
        path, role="publisher", expected_store_id=snapshot.store_id,
        expected_revision=snapshot.revision, expected_scope=("stream:fixture",),
    )
    with pytest.raises(EnvelopeOpenError):
        accept_envelope_file(
            env["sealed"], history_output,
            expected_store_id=snapshot.store_id,
            expected_revision=snapshot.revision, **args,
        )
    assert not history_output.exists()
    assert open_historical_envelope_file(
        env["sealed"], history_output,
        signer_fingerprint=env["signer"], **args,
    ).read_bytes() == b"accepted original\x00bytes"
    changed = env["sealed"].with_name("changed.pgp")
    changed.write_bytes(env["sealed"].read_bytes() + b"x")
    with pytest.raises(EnvelopeOpenError):
        open_historical_envelope_file(
            changed, history_output.with_name("unaccepted.bin"),
            signer_fingerprint=env["signer"], **args,
        )
    assert not history_output.with_name("unaccepted.bin").exists()


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_file_receipt_rolls_back_output_if_database_write_fails(
    stream_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
):
    env = stream_env
    env["source"].write_bytes(b"must not escape failed acceptance")
    env["source"].chmod(0o600)
    _seal(env)
    path, snapshot = _trust_for_file(env, tmp_path, monkeypatch)
    real_connect = trust_state._connect

    class FailingConnection:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *arguments):
            if failure == "commit" and arguments[0] is None and env["released"].exists():
                self.db.rollback()
                raise sqlite3.OperationalError("fixture commit failure")
            return self.db.__exit__(*arguments)

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, parameters=()):
            if failure == "insert" and sql.startswith("INSERT INTO receipts VALUES"):
                assert env["released"].read_bytes() == b"must not escape failed acceptance"
                raise sqlite3.OperationalError("fixture write failure")
            return self.db.execute(sql, parameters)

    monkeypatch.setattr(trust_state, "_connect", lambda selected: FailingConnection(real_connect(selected)))
    with pytest.raises(EnvelopeOpenError) as failed:
        accept_envelope_file(
            env["sealed"], env["released"], limits=env["limits"],
            trust_state_path=path, expected_scope=("stream:fixture",),
            role="publisher", expected_store_id=snapshot.store_id,
            expected_revision=snapshot.revision, gnupg_home=env["receiver"],
            policy=env["policy"], authorized=True,
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    assert failed.value.code.value == "AUTHORIZATION_DENIED"
    assert not env["released"].exists()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_current_file_accept_rechecks_revocation_before_naming_output(
    stream_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    env = stream_env
    env["source"].write_bytes(b"no release after local revocation")
    env["source"].chmod(0o600)
    _seal(env)
    path, snapshot = _trust_for_file(env, tmp_path, monkeypatch)
    real_strip = envelope_io._strip_inner_prefix

    def revoke_after_crypto(*args, **kwargs):
        real_strip(*args, **kwargs)
        trust_state.revoke_authority(
            path, role="publisher", expected_store_id=snapshot.store_id,
            expected_revision=snapshot.revision,
            expected_scope=("stream:fixture",),
        )

    monkeypatch.setattr(envelope_io, "_strip_inner_prefix", revoke_after_crypto)
    with pytest.raises(EnvelopeOpenError) as denied:
        accept_envelope_file(
            env["sealed"], env["released"], limits=env["limits"],
            trust_state_path=path, expected_scope=("stream:fixture",),
            role="publisher", expected_store_id=snapshot.store_id,
            expected_revision=snapshot.revision, gnupg_home=env["receiver"],
            policy=env["policy"], authorized=True,
            passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        )
    assert denied.value.code.value == "AUTHORIZATION_DENIED"
    assert not env["released"].exists()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["current", "historical"])
@pytest.mark.parametrize("change", ["source_after_strip", "source_after_publish", "cancel_after_publish"])
def test_receipted_file_rejects_late_changes(stream_env, tmp_path, monkeypatch, mode, change):
    env = stream_env
    env["source"].write_bytes(b"exact accepted payload")
    env["source"].chmod(0o600)
    _seal(env)
    path, snapshot = _trust_for_file(env, tmp_path, monkeypatch)
    args = dict(
        limits=env["limits"], trust_state_path=path, expected_scope=("stream:fixture",),
        role="publisher", gnupg_home=env["receiver"], policy=env["policy"], authorized=True,
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
    )
    current = dict(expected_store_id=snapshot.store_id, expected_revision=snapshot.revision)
    if mode == "historical":
        accept_envelope_file(env["sealed"], env["released"], **current, **args)
    target = env["released"].with_name("late.bin")
    cancelled = False

    def change_source():
        old = env["sealed"].with_name("retained.pgp")
        env["sealed"].rename(old)
        env["sealed"].write_bytes(b"replacement must survive")

    original_strip = envelope_io._strip_inner_prefix
    original_publish = envelope_io.ExclusiveOutput.publish

    def strip(*positional, **keywords):
        original_strip(*positional, **keywords)
        if change == "source_after_strip":
            change_source()

    def publish(self, name):
        nonlocal cancelled
        result = original_publish(self, name)
        if change == "source_after_publish":
            change_source()
        if change == "cancel_after_publish":
            cancelled = True
        return result

    monkeypatch.setattr(envelope_io, "_strip_inner_prefix", strip)
    monkeypatch.setattr(envelope_io.ExclusiveOutput, "publish", publish)
    args["cancel_check"] = lambda: cancelled
    with pytest.raises(EnvelopeOpenError):
        if mode == "historical":
            open_historical_envelope_file(
                env["sealed"], target, signer_fingerprint=env["signer"], **args,
            )
        else:
            accept_envelope_file(env["sealed"], target, **current, **args)
    assert not target.exists()
    assert not tuple(target.parent.glob(".ls-openpgp-*"))
    if change != "cancel_after_publish":
        assert env["sealed"].read_bytes() == b"replacement must survive"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == int(mode == "historical")


def test_accept_file_budget_includes_initial_authority_query(stream_env, monkeypatch):
    env = stream_env
    now = [100.0]
    monkeypatch.setattr(envelope_io, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def slow_authority(*args, **kwargs):
        now[0] += env["limits"].timeout_seconds + 1
        return SimpleNamespace(store_id="store", revision=1, scope=("fixture",))

    monkeypatch.setattr(trust_state, "authorize_inbound_peer", slow_authority)
    with pytest.raises(EnvelopeOpenError) as denied:
        accept_envelope_file(
            env["sealed"], env["released"], limits=env["limits"],
            trust_state_path="unused", expected_scope=("fixture",), role="publisher",
            expected_store_id="store", expected_revision=1, gnupg_home=env["receiver"],
            policy=env["policy"], authorized=True,
        )
    assert denied.value.code.value == "GPG_TIMEOUT"
    assert not env["released"].exists()


def test_schema_one_legacy_inner_fixture_opens_in_both_apis(stream_env):
    """011439a0's canonical inner bytes remain readable without live Git history."""
    env = stream_env
    payload = b"legacy schema one\x00payload"
    digest = hashlib.sha256(payload).hexdigest().encode("ascii")
    header = b'{"format":"localsetup.openpgp-envelope","schema_version":1}'
    manifest = (
        b'{"header":"{\\"format\\":\\"localsetup.openpgp-envelope\\",'
        b'\\"schema_version\\":1}","payload_length":'
        + str(len(payload)).encode("ascii")
        + b',"payload_sha256":"' + digest
        + b'","recipient_fingerprints":["' + env["recipient"].encode("ascii")
        + b'"],"signer_fingerprint":"' + env["signer"].encode("ascii") + b'"}'
    )
    inner = b"localsetup.openpgp-inner\x00\x01" + struct.pack(">I", len(manifest)) + manifest + payload
    passphrase_file = env["source"].with_name("fixture-passphrase")
    passphrase_file.write_text(PASSPHRASE + "\n")
    passphrase_file.chmod(0o600)
    try:
        ciphertext = _gpg(
            env["sender"], "--pinentry-mode", "loopback",
            "--passphrase-file", str(passphrase_file),
            "--no-encrypt-to", "--trust-model", "always",
            "--set-filename", "", "--throw-keyids", "--no-armor",
            "--local-user", env["signer"], "--recipient", env["recipient"],
            "--output", "-", "--sign", "--encrypt", "-", input_data=inner,
        )
    finally:
        passphrase_file.unlink()
    legacy = struct.pack(">H", len(header)) + header + ciphertext
    env["sealed"].write_bytes(legacy)
    assert _open(env).read_bytes() == payload
    assert open_envelope(
        legacy, gnupg_home=env["receiver"], policy=env["policy"],
        local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
    ) == payload
    env["source"].write_bytes(payload)
    env["source"].chmod(0o600)
    env["sealed"].unlink()
    _seal(env)
    assert open_envelope(
        env["sealed"].read_bytes(), gnupg_home=env["receiver"],
        policy=env["policy"], local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
    ) == payload


def test_optional_file_seal_disables_compression_without_changing_default(stream_env):
    env = stream_env
    env["source"].write_bytes(b"compressible" * 100_000)
    env["source"].chmod(0o600)
    default_path = _seal(env)
    plain_path = default_path.with_name("no-compression.pgp")
    seal_envelope_file(
        env["source"], plain_path, limits=env["limits"],
        gnupg_home=env["sender"], signer_fingerprint=env["signer"],
        recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}),
        passphrase_reference=env["reference"], secret_resolver=env["resolver"],
        disable_compression=True,
    )
    assert plain_path.stat().st_size > default_path.stat().st_size
    _open(env, sealed=default_path, released=env["released"])
    assert env["released"].read_bytes() == env["source"].read_bytes()
    second = env["released"].with_name("no-compression.bin")
    _open(env, sealed=plain_path, released=second)
    assert second.read_bytes() == env["source"].read_bytes()


def test_native_file_seal_cancellation_requests_no_gpg_or_staging(stream_env, monkeypatch):
    env = stream_env
    env["source"].write_bytes(b"native cancellation source")
    env["source"].chmod(0o600)
    operation = describe_seal_file_operation(
        source_path=env["source"], output_path=env["sealed"], limits=env["limits"],
        signer_fingerprint=env["signer"], recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}), gnupg_home=env["sender"],
    )
    native = _NativeInputFixture(operation, cancel=True)
    monkeypatch.setattr(
        envelope_io, "run_gpg_file",
        lambda *_args, **_kwargs: pytest.fail("cancelled input launched GnuPG"),
    )
    with pytest.raises(EnvelopeError) as cancelled:
        seal_envelope_file(
            env["source"], env["sealed"], limits=env["limits"],
            gnupg_home=env["sender"], signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}), passphrase_reference=None,
            secret_resolver=SecretResolver(), native_input=native,
        )
    assert cancelled.value.code is EnvelopeErrorCode.PASSPHRASE_UNAVAILABLE
    assert native.request_count == 1
    assert native.finished == 1
    assert not env["sealed"].exists()
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))


def test_anchored_file_and_output_reject_replaced_parent_chains(tmp_path: Path):
    source_directory = tmp_path / "source"
    output_directory = tmp_path / "output"
    source_directory.mkdir(mode=0o700)
    output_directory.mkdir(mode=0o700)
    source = source_directory / "payload.bin"
    output_path = output_directory / "released.bin"
    source.write_bytes(b"unchanged inode")
    source.chmod(0o600)
    anchored_source = envelope_io.AnchoredFile(source, private=True)
    anchored_output = envelope_io.ExclusiveOutput(output_path)
    moved_source = source_directory.with_name("source-original")
    moved_output = output_directory.with_name("output-original")
    try:
        source_directory.rename(moved_source)
        source_directory.mkdir(mode=0o700)
        os.replace(moved_source / source.name, source)
        output_directory.rename(moved_output)
        output_directory.mkdir(mode=0o700)

        assert not anchored_source.unchanged()
        assert not anchored_output.unchanged()
        with pytest.raises(OSError):
            anchored_output.stage()
    finally:
        anchored_source.close()
        anchored_output.close()


def test_exclusive_output_keeps_stage_and_destination_replacements(tmp_path: Path):
    parent = tmp_path / "private-output"
    parent.mkdir(mode=0o700)
    destination = parent / "output.bin"
    output = envelope_io.ExclusiveOutput(destination)
    stage_name, stage_fd = output.stage()
    os.write(stage_fd, b"owned stage")
    os.close(stage_fd)
    stage_path = parent / stage_name
    displaced = parent / "displaced-stage"
    stage_path.rename(displaced)
    stage_path.write_bytes(b"replacement stage")
    stage_path.chmod(0o600)

    try:
        with pytest.raises(OSError):
            output.publish(stage_name)
        assert not destination.exists()
        assert stage_path.read_bytes() == b"replacement stage"

        destination.write_bytes(b"pre-existing destination")
        destination.chmod(0o600)
        with pytest.raises(OSError):
            output.publish(stage_name)
        assert destination.read_bytes() == b"pre-existing destination"
        assert stage_path.read_bytes() == b"replacement stage"
    finally:
        output.close()

    assert stage_path.read_bytes() == b"replacement stage"
    assert destination.read_bytes() == b"pre-existing destination"


@pytest.mark.skipif(
    not envelope_io._fd_publication_available(), reason="fd-bound publication unavailable",
)
def test_exclusive_output_links_held_stage_when_name_changes_inside_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "private-output"
    parent.mkdir(mode=0o700)
    destination = parent / "output.bin"
    output = envelope_io.ExclusiveOutput(destination)
    stage_name, stage_fd = output.stage()
    os.write(stage_fd, b"owned stage")
    os.close(stage_fd)
    stage_path = parent / stage_name
    displaced = parent / "displaced-stage"
    real_link = os.link

    def replace_inside_link(source, target, **kwargs):
        assert source.startswith("/proc/self/fd/")
        assert kwargs["follow_symlinks"] is True
        stage_path.rename(displaced)
        stage_path.write_bytes(b"replacement stage")
        stage_path.chmod(0o600)
        real_link(source, target, **kwargs)
        assert destination.read_bytes() == b"owned stage"

    monkeypatch.setattr(envelope_io, "_fd_publication_available", lambda: True)
    monkeypatch.setattr(envelope_io.os, "link", replace_inside_link)
    try:
        with pytest.raises(OSError, match="stage identity changed"):
            output.publish(stage_name)
        assert not destination.exists()
        assert stage_path.read_bytes() == b"replacement stage"
        assert displaced.read_bytes() == b"owned stage"
    finally:
        output.close()
    assert stage_path.read_bytes() == b"replacement stage"


@pytest.mark.skipif(
    not envelope_io._fd_publication_available(), reason="fd-bound publication unavailable",
)
def test_exclusive_output_preserves_replacement_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "private-output"
    parent.mkdir(mode=0o700)
    destination = parent / "output.bin"
    output = envelope_io.ExclusiveOutput(destination)
    stage_name, stage_fd = output.stage()
    os.write(stage_fd, b"owned stage")
    os.close(stage_fd)
    real_link = os.link

    def replace_output_after_link(source, target, **kwargs):
        real_link(source, target, **kwargs)
        destination.unlink()
        destination.write_bytes(b"unknown replacement")
        destination.chmod(0o600)

    monkeypatch.setattr(envelope_io, "_fd_publication_available", lambda: True)
    monkeypatch.setattr(envelope_io.os, "link", replace_output_after_link)
    try:
        with pytest.raises(OSError, match="stage identity changed"):
            output.publish(stage_name)
        assert destination.read_bytes() == b"unknown replacement"
    finally:
        output.close()
    assert destination.read_bytes() == b"unknown replacement"


def test_exclusive_output_rejects_unavailable_fd_publication_before_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "private-output"
    parent.mkdir(mode=0o700)
    destination = parent / "output.bin"
    output = envelope_io.ExclusiveOutput(destination)
    stage_name, stage_fd = output.stage()
    os.close(stage_fd)
    monkeypatch.setattr(envelope_io, "_fd_publication_available", lambda: False)
    monkeypatch.setattr(
        envelope_io.os, "link", lambda *_args, **_kwargs: pytest.fail("link attempted"),
    )
    try:
        with pytest.raises(EnvelopeError) as unavailable:
            output.publish(stage_name)
        assert unavailable.value.code is EnvelopeErrorCode.FILE_PUBLICATION_UNAVAILABLE
        assert not destination.exists()
    finally:
        output.close()


def test_native_file_seal_rehashes_source_after_input_and_wipes_buffer(
    stream_env, monkeypatch,
):
    env = stream_env
    env["source"].write_bytes(b"native source before prompt")
    env["source"].chmod(0o600)
    operation = describe_seal_file_operation(
        source_path=env["source"], output_path=env["sealed"], limits=env["limits"],
        signer_fingerprint=env["signer"], recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}), gnupg_home=env["sender"],
    )

    def change_source():
        env["source"].write_bytes(b"native source after prompt")
        return bytearray(PASSPHRASE.encode("utf-8"))

    native = _NativeInputFixture(operation, on_request=change_source)
    monkeypatch.setattr(
        envelope_io, "run_gpg_file",
        lambda *_args, **_kwargs: pytest.fail("changed input launched GnuPG"),
    )
    with pytest.raises(EnvelopeError) as changed:
        seal_envelope_file(
            env["source"], env["sealed"], limits=env["limits"],
            gnupg_home=env["sender"], signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}), passphrase_reference=None,
            secret_resolver=SecretResolver(), native_input=native,
        )
    assert changed.value.code is EnvelopeErrorCode.INVALID_INPUT
    assert native.request_count == 1
    assert native.finished == 1
    assert native.buffers and not any(native.buffers[0])
    assert not env["sealed"].exists()
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))


def test_native_file_descriptor_detects_source_moved_into_replacement_parent(
    stream_env, monkeypatch,
):
    env = stream_env
    env["source"].write_bytes(b"same inode, replaced parent")
    env["source"].chmod(0o600)
    original_binding = envelope_io._inspect_native_file_binding(
        env["source"], env["sealed"], limits=env["limits"], private_source=True,
    )
    operation = describe_seal_file_operation(
        source_path=env["source"], output_path=env["sealed"], limits=env["limits"],
        signer_fingerprint=env["signer"], recipient_fingerprints=(env["recipient"],),
        local_trust=LocalTrust({env["signer"]}), gnupg_home=env["sender"],
    )
    source_directory = env["source"].parent
    moved_directory = source_directory.with_name(source_directory.name + "-original")
    source_directory.rename(moved_directory)
    source_directory.mkdir(mode=0o700)
    os.replace(moved_directory / env["source"].name, env["source"])
    replacement_binding = envelope_io._inspect_native_file_binding(
        env["source"], env["sealed"], limits=env["limits"], private_source=True,
    )
    assert replacement_binding["source"]["parent"] != original_binding["source"]["parent"]
    assert replacement_binding["source"]["identity"]["device"] == original_binding["source"]["identity"]["device"]
    assert replacement_binding["source"]["identity"]["inode"] == original_binding["source"]["identity"]["inode"]
    native = _NativeInputFixture(operation)
    monkeypatch.setattr(
        envelope_io, "run_gpg_file",
        lambda *_args, **_kwargs: pytest.fail("descriptor mismatch launched GnuPG"),
    )
    with pytest.raises(EnvelopeError):
        seal_envelope_file(
            env["source"], env["sealed"], limits=env["limits"],
            gnupg_home=env["sender"], signer_fingerprint=env["signer"],
            recipient_fingerprints=(env["recipient"],),
            local_trust=LocalTrust({env["signer"]}), passphrase_reference=None,
            secret_resolver=SecretResolver(), native_input=native,
        )
    assert native.request_count == 0
    assert native.finished == 0
    assert not env["sealed"].exists()
    assert not tuple(env["sealed"].parent.glob(".ls-openpgp-*"))


def test_native_file_open_rejects_unbound_receipt_and_historical_modes(stream_env):
    env = stream_env
    native = _NativeInputFixture(None)
    shared = dict(
        limits=env["limits"], gnupg_home=env["receiver"], policy=env["policy"],
        local_trust=LocalTrust({env["signer"]}), authorized=True,
        passphrase_reference=None, secret_resolver=SecretResolver(), cancel_check=None,
        native_input=native,
    )
    for extra in (
        {"receipt_callback": lambda *_args: None},
        {"allow_historical_signature": True},
        {"expected_ciphertext_sha256": "0" * 64},
    ):
        with pytest.raises(EnvelopeOpenError) as rejected:
            envelope_io.open_file(
                env["sealed"], env["released"], **shared, **extra,
            )
        assert rejected.value.code.value == "INVALID_INPUT"
    assert native.request_count == 0

from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from ls.core.openpgp import KeyCapability, KeyProfile
from ls.core.openpgp import keys as keys_api
from ls.core.openpgp import recovery as recovery_api
from ls.core.openpgp import recovery_process as recovery_process_api
from ls.core.openpgp.keys import KeyInspection, KeyRecord
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver

OWNER = "A" * 40
OWNER_ENCRYPTION = "B" * 40
RECOVERY = "C" * 40
RECOVERY_ENCRYPTION = "D" * 40
UNEXPECTED = "E" * 40
OWNER_PASSPHRASE = "fixture-owner-passphrase"
RECOVERY_PASSPHRASE = "fixture-recovery-passphrase"
OWNER_REFERENCE = SecretReference(SecretProvider.ENV, "OPENPGP_OWNER_TEST_PASSPHRASE")
RECOVERY_REFERENCE = SecretReference(
    SecretProvider.ENV, "OPENPGP_RECOVERY_TEST_PASSPHRASE"
)
OWNER_PACKET = bytes(range(1, 65))
UNEXPECTED_PACKET = bytes(range(65, 129))
PUBLIC_OWNER_PACKET = bytes(range(129, 193))
EXTRA_PRIMARY_PACKET = bytes(range(192, 256))
STUB_OWNER_PACKET = b"TEST-ONLY-DUMMY-OWNER-SECRET"
_CIPHER_PREFIX = b"TEST-ONLY-SEALED\x00"


class _Resolver(SecretResolver):
    def resolve(self, reference: SecretReference) -> str:
        if reference == OWNER_REFERENCE:
            return OWNER_PASSPHRASE
        if reference == RECOVERY_REFERENCE:
            return RECOVERY_PASSPHRASE
        raise AssertionError("unexpected secret reference")



class _StatefulPath:
    def __init__(self, first: Path, later: Path) -> None:
        self.first = first
        self.later = later
        self.calls = 0

    def __fspath__(self) -> str:
        self.calls += 1
        return str(self.first if self.calls == 1 else self.later)

def _epoch(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())


def _inspection(fingerprint: str):
    created = date.today()
    expires = KeyProfile().expiry_date(created)
    recovery_identity = fingerprint == RECOVERY
    primary_capabilities = (
        frozenset({KeyCapability.CERTIFY})
        if recovery_identity
        else frozenset({KeyCapability.SIGN, KeyCapability.CERTIFY})
    )
    subkey_fingerprint = RECOVERY_ENCRYPTION if recovery_identity else OWNER_ENCRYPTION

    return KeyInspection(
        primary=KeyRecord(
            fingerprint=fingerprint,
            algorithm="RSA",
            bits=4096,
            capabilities=primary_capabilities,
            direct_capabilities=primary_capabilities,
            created_on=created,
            expires_on=expires,
            expires_epoch=_epoch(expires),
            is_primary=True,
        ),
        subkeys=(
            KeyRecord(
                fingerprint=subkey_fingerprint,
                algorithm="RSA",
                bits=4096,
                capabilities=frozenset({KeyCapability.ENCRYPT}),
                direct_capabilities=frozenset({KeyCapability.ENCRYPT}),
                created_on=created,
                expires_on=expires,
                expires_epoch=_epoch(expires),
                is_primary=False,
            ),
        ),
        _verification=keys_api._INSPECTION_SEAL,
    )


def _ciphertext(payload: bytes) -> bytes:
    digest = hashlib.sha256(payload).digest()
    return _CIPHER_PREFIX + digest + bytes(value ^ 0xA5 for value in payload)


def _prepare_fake_gpg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path]:
    owner_home = tmp_path / "owner-home"
    recovery_public_home = tmp_path / "recovery-public-home"
    recovery_private_home = tmp_path / "recovery-private-home"
    for home in (owner_home, recovery_public_home, recovery_private_home):
        home.mkdir(mode=0o700)
    for home, fingerprint in (
        (owner_home, OWNER),
        (recovery_public_home, RECOVERY),
        (recovery_private_home, RECOVERY),
    ):
        marker = home / ".fixture-identity"
        marker.write_text(fingerprint, encoding="ascii")
        marker.chmod(0o600)
    secret_marker = recovery_private_home / ".fixture-secret-primary"
    secret_marker.write_text(RECOVERY, encoding="ascii")
    secret_marker.chmod(0o600)

    owner_inspection = _inspection(OWNER)
    recovery_inspection = _inspection(RECOVERY)

    def inspect(
        *,
        certificate=None,
        keyring_home=None,
        fingerprint=None,
        gpg_binary="gpg",
    ):
        del certificate, gpg_binary
        if fingerprint is None:
            raise AssertionError("inspection must pin the full fingerprint")
        if keyring_home is None:
            raise AssertionError("keyring home must be explicit")
        selected_home = Path(keyring_home)
        if selected_home == owner_home:
            if fingerprint != OWNER:
                raise AssertionError("unexpected owner fingerprint")
            return owner_inspection
        if selected_home in (recovery_public_home, recovery_private_home):
            if fingerprint != RECOVERY:
                raise AssertionError("unexpected recovery fingerprint")
            return recovery_inspection
        if fingerprint != OWNER:
            raise AssertionError("unexpected restored fingerprint")
        restored = selected_home / ".fixture-primary"
        return _inspection(restored.read_text(encoding="ascii"))

    def secret_record(fingerprint: str, *, availability: str = "+") -> bytes:
        selected = {
            OWNER: OWNER_ENCRYPTION,
            RECOVERY: RECOVERY_ENCRYPTION,
        }.get(fingerprint)
        output = (
            f"sec:u:4096:1:{fingerprint[-16:]}:1:9999999999:::::sc:::{availability}:\n"
            f"fpr:::::::::{fingerprint}:\n"
        )
        if selected is not None:
            output += (
                f"ssb:u:4096:1:{selected[-16:]}:1:9999999999:::::e:::{availability}:\n"
                f"fpr:::::::::{selected}:\n"
            )
        return output.encode("ascii")

    def list_secret_keys(
        _executable: str, home: Path, arguments: tuple[str, ...]
    ) -> bytes:
        if arguments[-1] == "--list-secret-keys":
            if Path(home) == recovery_public_home:
                return b""
            primary_marker = Path(home) / ".fixture-secret-primary"
            if not primary_marker.exists():
                return b""
            fingerprints = [primary_marker.read_text(encoding="ascii")]
            extra_marker = Path(home) / ".fixture-extra-secret-primary"
            if extra_marker.exists():
                fingerprints.append(extra_marker.read_text(encoding="ascii"))
            marker = "#" if (Path(home) / ".fixture-secret-stub").exists() else "+"
            return b"".join(secret_record(fingerprint, availability=marker) for fingerprint in fingerprints)
        return secret_record(arguments[-1])

    monkeypatch.setattr(recovery_api, "inspect_key", inspect)
    monkeypatch.setattr(
        recovery_process_api, "_run_secret_listing_gpg", list_secret_keys
    )
    monkeypatch.setattr(
        recovery_process_api, "_home_agent_is_running", lambda _home: False
    )
    monkeypatch.setattr(
        recovery_process_api, "_stop_home_agent", lambda _home: True
    )

    fake_gpg = tmp_path / "fixture-gpg"
    fake_gpg.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if any(secret in ' '.join(args) for secret in (\n"
        "    'fixture-owner-passphrase', 'fixture-recovery-passphrase')):\n"
        "    raise SystemExit(39)\n"
        "home = pathlib.Path(args[args.index('--homedir') + 1])\n"
        "if '--list-packets' in args:\n"
        "    if '--list-only' not in args or args[-1] != '-':\n"
        "        raise SystemExit(45)\n"
        "    data = sys.stdin.buffer.read()\n"
        f"    keyid = '{RECOVERY_ENCRYPTION[-16:]}'\n"
        "    if data.startswith(b'UNEXPECTED-RECIPIENT'):\n"
        f"        keyid = '{UNEXPECTED[-16:]}'\n"
        "    print(f'pubkey enc packet keyid {keyid}')\n"
        "elif '--export-secret-keys' in args:\n"
        "    fd = int(args[args.index('--passphrase-fd') + 1])\n"
        "    if os.read(fd, 8192) != b'fixture-owner-passphrase\\n':\n"
        "        raise SystemExit(40)\n"
        "    sys.stdout.buffer.write(bytes(range(1, 65)))\n"
        "elif '--encrypt' in args:\n"
        "    data = sys.stdin.buffer.read()\n"
        "    sealed = b'TEST-ONLY-SEALED\\x00' + hashlib.sha256(data).digest()\n"
        "    sealed += bytes(value ^ 0xA5 for value in data)\n"
        "    sys.stdout.buffer.write(sealed)\n"
        "elif '--decrypt' in args:\n"
        "    fd = int(args[args.index('--passphrase-fd') + 1])\n"
        "    if os.read(fd, 8192) != b'fixture-recovery-passphrase\\n':\n"
        "        raise SystemExit(41)\n"
        "    sealed = sys.stdin.buffer.read()\n"
        "    prefix = b'TEST-ONLY-SEALED\\x00'\n"
        "    if not sealed.startswith(prefix) or len(sealed) < len(prefix) + 32:\n"
        "        raise SystemExit(42)\n"
        "    digest = sealed[len(prefix):len(prefix) + 32]\n"
        "    data = bytes(value ^ 0xA5 for value in sealed[len(prefix) + 32:])\n"
        "    if hashlib.sha256(data).digest() != digest:\n"
        "        raise SystemExit(43)\n"
        "    sys.stdout.buffer.write(data)\n"
        "elif '--import' in args:\n"
        "    data = sys.stdin.buffer.read()\n"
        "    if data == bytes(range(1, 65)):\n"
        f"        fingerprint = '{OWNER}'\n"
        "        secret_fingerprints = (fingerprint,)\n"
        "    elif data == bytes(range(65, 129)):\n"
        f"        fingerprint = '{UNEXPECTED}'\n"
        "        secret_fingerprints = (fingerprint,)\n"
        "    elif data == bytes(range(129, 193)):\n"
        f"        fingerprint = '{OWNER}'\n"
        "        secret_fingerprints = ()\n"
        f"    elif data == {STUB_OWNER_PACKET!r}:\n"
        f"        fingerprint = '{OWNER}'\n"
        "        secret_fingerprints = (fingerprint,)\n"
        "        (home / '.fixture-secret-stub').write_text('dummy', encoding='ascii')\n"
        "    elif data == bytes(range(192, 256)):\n"
        f"        fingerprint = '{OWNER}'\n"
        f"        secret_fingerprints = (fingerprint, '{UNEXPECTED}')\n"
        "    else:\n"
        "        raise SystemExit(44)\n"
        "    marker = home / '.fixture-primary'\n"
        "    marker.write_text(fingerprint, encoding='ascii')\n"
        "    marker.chmod(0o600)\n"
        "    if secret_fingerprints:\n"
        "        secret_marker = home / '.fixture-secret-primary'\n"
        "        secret_marker.write_text(secret_fingerprints[0], encoding='ascii')\n"
        "        secret_marker.chmod(0o600)\n"
        "    if len(secret_fingerprints) > 1:\n"
        "        extra_marker = home / '.fixture-extra-secret-primary'\n"
        "        extra_marker.write_text(secret_fingerprints[1], encoding='ascii')\n"
        "        extra_marker.chmod(0o600)\n"
        "    (home / 'pubring.kbx').write_bytes(b'fixture-keybox')\n",
        encoding="utf-8",
    )
    fake_gpg.chmod(0o700)
    return owner_home, recovery_public_home, recovery_private_home


def _backup(
    owner_home: Path,
    recovery_public_home: Path,
    destination: Path,
    gpg: Path,
    *,
    resolver: SecretResolver | None = None,
):
    return recovery_api.create_protected_backup(
        keyring_home=owner_home,
        expected_primary_fingerprint=OWNER,
        recovery_public_keyring_home=recovery_public_home,
        recovery_recipient_fingerprint=RECOVERY,
        owner_passphrase_reference=OWNER_REFERENCE,
        secret_resolver=resolver if resolver is not None else _Resolver(),
        backup_path=destination,
        profile=KeyProfile(),
        required_capabilities={KeyCapability.SIGN, KeyCapability.ENCRYPT},
        gpg_binary=gpg,
    )


def _restore(
    backup: str | os.PathLike[str],
    recovery_home: Path,
    target_home: Path,
    gpg: Path,
):
    return recovery_api.restore_protected_backup(
        backup_path=backup,
        recovery_keyring_home=recovery_home,
        recovery_recipient_fingerprint=RECOVERY,
        keyring_home=target_home,
        expected_primary_fingerprint=OWNER,
        recovery_passphrase_reference=RECOVERY_REFERENCE,
        secret_resolver=_Resolver(),
        profile=KeyProfile(),
        required_capabilities={KeyCapability.SIGN, KeyCapability.ENCRYPT},
        gpg_binary=gpg,
    )


def test_backup_streams_secret_export_into_ciphertext_and_restores_isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_home, recovery_public_home, recovery_private_home = _prepare_fake_gpg(
        monkeypatch, tmp_path
    )
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "owner.gpg"
    target_home = tmp_path / "restored-home"

    receipt = _backup(owner_home, recovery_public_home, backup_path, gpg)
    restored = _restore(backup_path, recovery_private_home, target_home, gpg)

    assert receipt.primary_fingerprint == OWNER
    assert receipt.recovery_recipient_fingerprint == RECOVERY
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    assert _CIPHER_PREFIX in backup_path.read_bytes()
    assert OWNER_PACKET not in backup_path.read_bytes()
    assert restored.keyring_home == target_home
    assert restored.primary_fingerprint == OWNER
    assert target_home != owner_home
    assert target_home != recovery_public_home
    assert target_home != recovery_private_home
    assert sorted(path.name for path in recovery_public_home.iterdir()) == [
        ".fixture-identity"
    ]
    assert stat.S_IMODE(target_home.stat().st_mode) == 0o700
    assert (target_home / ".fixture-primary").read_text(encoding="ascii") == OWNER
    assert stat.S_IMODE((target_home / "pubring.kbx").stat().st_mode) == 0o600
    assert not any(
        OWNER_PACKET in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_tampered_ciphertext_fails_without_importing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_home, recovery_public_home, recovery_private_home = _prepare_fake_gpg(
        monkeypatch, tmp_path
    )
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "owner.gpg"
    target_home = tmp_path / "tampered-restore"
    _backup(owner_home, recovery_public_home, backup_path, gpg)
    tampered = bytearray(backup_path.read_bytes())
    tampered[-1] ^= 1
    backup_path.write_bytes(tampered)
    backup_path.chmod(0o600)

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, gpg)

    assert raised.value.code is recovery_api.RecoveryErrorCode.GPG_FAILED
    assert list(target_home.iterdir()) == []


def test_restore_rejects_different_primary_fingerprint_and_clears_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, recovery_private_home = _prepare_fake_gpg(monkeypatch, tmp_path)
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "wrong-key.gpg"
    backup_path.write_bytes(_ciphertext(UNEXPECTED_PACKET))
    backup_path.chmod(0o600)
    target_home = tmp_path / "wrong-key-restore"

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, gpg)

    assert raised.value.code is recovery_api.RecoveryErrorCode.FINGERPRINT_MISMATCH
    assert list(target_home.iterdir()) == []


def test_restore_rejects_public_only_owner_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, recovery_private_home = _prepare_fake_gpg(monkeypatch, tmp_path)
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "public-owner.gpg"
    backup_path.write_bytes(_ciphertext(PUBLIC_OWNER_PACKET))
    backup_path.chmod(0o600)
    target_home = tmp_path / "public-only-restore"

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, gpg)

    assert raised.value.code is recovery_api.RecoveryErrorCode.OWNER_KEY_INVALID
    assert list(target_home.iterdir()) == []


def test_restore_rejects_dummy_secret_owner_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, recovery_private_home = _prepare_fake_gpg(monkeypatch, tmp_path)
    backup_path = tmp_path / "stub-owner.gpg"
    backup_path.write_bytes(_ciphertext(STUB_OWNER_PACKET))
    backup_path.chmod(0o600)
    target_home = tmp_path / "stub-owner-restore"

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, tmp_path / "fixture-gpg")

    assert raised.value.code is recovery_api.RecoveryErrorCode.OWNER_KEY_INVALID
    assert list(target_home.iterdir()) == []


def test_restore_rejects_unexpected_secret_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, recovery_private_home = _prepare_fake_gpg(monkeypatch, tmp_path)
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "extra-owner.gpg"
    backup_path.write_bytes(_ciphertext(EXTRA_PRIMARY_PACKET))
    backup_path.chmod(0o600)
    target_home = tmp_path / "extra-primary-restore"

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, gpg)

    assert raised.value.code is recovery_api.RecoveryErrorCode.OWNER_KEY_INVALID
    assert list(target_home.iterdir()) == []


def test_restore_rejects_unexpected_recovery_secret_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, recovery_private_home = _prepare_fake_gpg(monkeypatch, tmp_path)
    extra_marker = recovery_private_home / ".fixture-extra-secret-primary"
    extra_marker.write_text(UNEXPECTED, encoding="ascii")
    extra_marker.chmod(0o600)
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "backup.gpg"
    backup_path.write_bytes(_ciphertext(OWNER_PACKET))
    backup_path.chmod(0o600)
    target_home = tmp_path / "recovery-with-extra-key"

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _restore(backup_path, recovery_private_home, target_home, gpg)

    assert raised.value.code is recovery_api.RecoveryErrorCode.RECOVERY_KEY_INVALID
    assert not target_home.exists()


def test_restore_uses_one_stateful_pathlike_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_home, recovery_public_home, recovery_private_home = _prepare_fake_gpg(
        monkeypatch, tmp_path
    )
    gpg = tmp_path / "fixture-gpg"
    backup_path = tmp_path / "owner.gpg"
    _backup(owner_home, recovery_public_home, backup_path, gpg)
    decoy_path = tmp_path / "decoy.gpg"
    decoy_path.write_bytes(b"UNEXPECTED-RECIPIENT")
    decoy_path.chmod(0o600)
    stateful_path = _StatefulPath(backup_path, decoy_path)
    target_home = tmp_path / "stateful-path-restore"

    restored = _restore(stateful_path, recovery_private_home, target_home, gpg)

    assert stateful_path.calls == 1
    assert restored.primary_fingerprint == OWNER


def test_owner_passphrase_resolution_error_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_home, recovery_public_home, _recovery_private_home = _prepare_fake_gpg(
        monkeypatch, tmp_path
    )

    class _FailingResolver(SecretResolver):
        def resolve(self, reference: SecretReference) -> str:
            assert reference == OWNER_REFERENCE
            raise RuntimeError("fixture-secret-must-not-escape")

    with pytest.raises(recovery_api.RecoveryError) as raised:
        _backup(
            owner_home,
            recovery_public_home,
            tmp_path / "uncreated.gpg",
            tmp_path / "fixture-gpg",
            resolver=_FailingResolver(),
        )

    assert raised.value.code is recovery_api.RecoveryErrorCode.SECRET_RESOLUTION_FAILED
    assert "fixture-secret-must-not-escape" not in str(raised.value)


def test_secret_listing_stops_only_the_agent_it_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "selected-home"
    home.mkdir(mode=0o700)
    agent_states = iter((False, True, False, True))
    stopped: list[Path] = []
    commands: list[list[str]] = []

    monkeypatch.setattr(
        recovery_process_api, "_home_agent_is_running", lambda _home: next(agent_states)
    )

    def run(command: list[str]) -> bytes:
        commands.append(command)
        return b"listed"

    monkeypatch.setattr(recovery_process_api, "_run_bounded_gpg", run)
    monkeypatch.setattr(
        recovery_process_api,
        "_stop_home_agent",
        lambda selected_home: stopped.append(selected_home) or True,
    )

    assert (
        recovery_process_api._run_secret_listing_gpg(
            "gpg", home, ("--list-secret-keys",)
        )
        == b"listed"
    )
    assert (
        recovery_process_api._run_secret_listing_gpg(
            "gpg", home, ("--list-secret-keys",)
        )
        == b"listed"
    )

    assert stopped == [home]
    assert all("--no-autostart" not in command for command in commands)
    assert all(
        command[command.index("--homedir") + 1] == str(home)
        for command in commands
    )


def test_agent_probe_does_not_start_the_selected_home_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "selected-home"
    home.mkdir(mode=0o700)
    commands: list[tuple[list[str], dict[str, object]]] = []
    responses = iter((b"", b"D 12345\nOK\n"))

    def run(command: list[str], **kwargs: object) -> bytes:
        commands.append((command, kwargs))
        return next(responses)

    monkeypatch.setattr(recovery_process_api, "_run_bounded_gpg", run)

    assert not recovery_process_api._home_agent_is_running(home)
    assert recovery_process_api._home_agent_is_running(home)
    assert [command for command, _ in commands] == [
        [
            "gpg-connect-agent",
            "--no-autostart",
            "--homedir",
            str(home),
            "GETINFO pid",
            "/bye",
        ]
    ] * 2
    assert all(options["timeout_seconds"] == 5.0 and options["output_limit"] == 128 for _, options in commands)


def test_agent_probe_rejects_unbounded_child_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "selected-home"
    home.mkdir(mode=0o700)
    helper = tmp_path / "gpg-connect-agent"
    helper.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'X' * 256)\n",
        encoding="ascii",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    with pytest.raises(recovery_api.RecoveryError) as raised:
        recovery_process_api._home_agent_is_running(home)
    assert raised.value.code is recovery_api.RecoveryErrorCode.IO_LIMIT


def test_secret_pipelines_stop_only_new_selected_home_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner_home = tmp_path / "owner-home"
    public_home = tmp_path / "public-home"
    recovery_home = tmp_path / "recovery-home"
    target_home = tmp_path / "target-home"
    for home in (owner_home, public_home, recovery_home, target_home):
        home.mkdir(mode=0o700)
    agent_states = {
        owner_home: False,
        public_home: False,
        recovery_home: False,
        target_home: False,
    }
    stopped: list[Path] = []
    runs = 0

    monkeypatch.setattr(
        recovery_process_api, "_home_agent_is_running", lambda home: agent_states[home]
    )

    def stop_agent(home: Path) -> bool:
        stopped.append(home)
        agent_states[home] = False
        return True

    monkeypatch.setattr(recovery_process_api, "_stop_home_agent", stop_agent)

    def run_pipeline(*_args, passphrase_write_fd: int, **_kwargs) -> None:
        nonlocal runs
        runs += 1
        os.close(passphrase_write_fd)
        if runs == 1:
            agent_states[owner_home] = True
            agent_states[public_home] = True
        elif runs == 2:
            agent_states[recovery_home] = True
            agent_states[target_home] = True
        else:
            agent_states[target_home] = True
            raise recovery_api.RecoveryError(recovery_api.RecoveryErrorCode.GPG_FAILED)

    monkeypatch.setattr(recovery_process_api, "_run_pipeline", run_pipeline)
    output_path = tmp_path / "ciphertext-output"
    output_descriptor = os.open(
        output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    backup_path = tmp_path / "backup-input"
    backup_path.write_bytes(b"fixture")
    backup = backup_path.open("rb")
    try:
        recovery_process_api._run_backup_pipeline(
            "gpg",
            owner_home,
            public_home,
            OWNER,
            RECOVERY,
            output_descriptor,
            bytearray(b"fixture\n"),
        )
        recovery_process_api._run_restore_pipeline(
            "gpg",
            recovery_home,
            target_home,
            backup,
            bytearray(b"fixture\n"),
        )
        agent_states[recovery_home] = True
        with pytest.raises(recovery_api.RecoveryError):
            recovery_process_api._run_restore_pipeline(
                "gpg",
                recovery_home,
                target_home,
                backup,
                bytearray(b"fixture\n"),
            )
    finally:
        backup.close()
        os.close(output_descriptor)

    assert stopped == [
        owner_home,
        public_home,
        recovery_home,
        target_home,
        target_home,
    ]
    assert agent_states == {
        owner_home: False,
        public_home: False,
        recovery_home: True,
        target_home: False,
    }

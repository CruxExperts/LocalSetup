from __future__ import annotations

import os
import stat
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from ls.core.openpgp import KeyCapability, KeyProfile
from ls.core.openpgp import generation as generation_api
from ls.core.openpgp.keys import KeyInspection, KeyRecord
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver


PRIMARY = "A" * 40
ENCRYPTION_SUBKEY = "B" * 40
CERTIFICATE = (
    b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    b"synthetic public test certificate\n"
    b"-----END PGP PUBLIC KEY BLOCK-----\n"
)
PASSPHRASE = "fixture-only-passphrase-do-not-log"
REFERENCE = SecretReference(SecretProvider.ENV, "OPENPGP_TEST_PASSPHRASE")


class _Resolver(SecretResolver):
    def __init__(self, value: str = PASSPHRASE) -> None:
        self.value = value
        self.calls = 0

    def resolve(self, reference: SecretReference) -> str:
        assert reference == REFERENCE
        self.calls += 1
        return self.value


def _inspection(
    created: date, expires: date | None, *, primary_bits: int = 4096
) -> KeyInspection:
    return KeyInspection(
        primary=KeyRecord(
            fingerprint=PRIMARY,
            algorithm="RSA",
            bits=primary_bits,
            capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
            ),
            direct_capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY}
            ),
            aggregate_capabilities=frozenset(
                {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
            ),
            created_on=created,
            expires_on=expires,
            is_primary=True,
        ),
        subkeys=(
            KeyRecord(
                fingerprint=ENCRYPTION_SUBKEY,
                algorithm="RSA",
                bits=4096,
                capabilities=frozenset({KeyCapability.ENCRYPT}),
                direct_capabilities=frozenset({KeyCapability.ENCRYPT}),
                created_on=created,
                expires_on=expires,
                is_primary=False,
            ),
        ),
    )


def _configure_generation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    created: date,
    expires: date,
) -> tuple[list[tuple[str, ...]], list[bytes | None], list[Path]]:
    calls: list[tuple[str, ...]] = []
    inspections = iter((_inspection(created, expires), _inspection(created, expires)))
    payloads: list[bytes | None] = []
    stopped_homes: list[Path] = []

    def run_gpg(
        _executable: str,
        keyring_home: Path,
        arguments: tuple[str, ...],
        *,
        passphrase: bytearray | None = None,
        timeout: float,
    ) -> bytes:
        del timeout
        calls.append(arguments)
        payloads.append(None if passphrase is None else bytes(passphrase))
        if "--generate-key" in arguments:
            batch_path = Path(arguments[arguments.index("--generate-key") + 1])
            assert stat.S_IMODE(batch_path.stat().st_mode) == 0o600
            assert PASSPHRASE.encode() not in batch_path.read_bytes()
            private_directory = keyring_home / "private-keys-v1.d"
            private_directory.mkdir(mode=0o700)
            private_file = private_directory / "synthetic-protected-key.fixture"
            private_file.write_bytes(b"synthetic protected-key fixture only")
            os.chmod(private_file, 0o644)
        if "--export" in arguments:
            return CERTIFICATE
        return b""

    def inspect_public_certificate(
        *, certificate: bytes, gpg_binary: str
    ) -> KeyInspection:
        assert certificate == CERTIFICATE
        assert gpg_binary == "fake-gpg"
        return next(inspections)

    monkeypatch.setattr(generation_api, "_run_gpg", run_gpg)
    monkeypatch.setattr(generation_api, "inspect_key", inspect_public_certificate)
    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stopped_homes.append)
    return calls, payloads, stopped_homes


def _generate(home: Path, resolver: SecretResolver) -> generation_api.GeneratedKey:
    return generation_api.generate_key(
        identity=generation_api.KeyIdentity(
            name="Synthetic fixture user",
            email="fixture@example.invalid",
            comment="isolated test",
        ),
        profile=KeyProfile(),
        passphrase_reference=REFERENCE,
        secret_resolver=resolver,
        keyring_home=home,
        gpg_binary="fake-gpg",
    )


def test_generation_enforces_leap_day_profile_and_keeps_private_store_protected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2028, 2, 29, tzinfo=tz)

    monkeypatch.setattr(generation_api, "datetime", FrozenDateTime)
    home = tmp_path / "isolated-gpg-home"
    calls, payloads, stopped_homes = _configure_generation(
        monkeypatch,
        created=date(2028, 2, 29),
        expires=date(2030, 2, 28),
    )
    resolver = _Resolver()

    generated = _generate(home, resolver)

    assert generated.public_certificate == CERTIFICATE
    assert generated.primary_fingerprint == PRIMARY
    assert generated.subkey_fingerprints == (ENCRYPTION_SUBKEY,)
    assert resolver.calls == 1
    assert stopped_homes == [home]
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    private_directory = home / "private-keys-v1.d"
    private_file = private_directory / "synthetic-protected-key.fixture"
    assert stat.S_IMODE(private_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_file.stat().st_mode) == 0o600
    assert not (home / ".localsetup-keygen.batch").exists()

    expiry_calls = [call for call in calls if "--quick-set-expire" in call]
    assert len(expiry_calls) == 2
    primary_position = expiry_calls[0].index("--quick-set-expire")
    assert expiry_calls[0][primary_position + 1 :] == (
        PRIMARY,
        "2030-02-28",
    )
    subkey_position = expiry_calls[1].index("--quick-set-expire")
    assert expiry_calls[1][subkey_position + 1 :] == (
        PRIMARY,
        "2030-02-28",
        ENCRYPTION_SUBKEY,
    )
    assert all("--export-secret-key" not in call for call in calls)
    assert "--export-secret-key" not in repr(generated)
    assert PASSPHRASE not in repr(generated)
    assert not any(PASSPHRASE in argument for call in calls for argument in call)
    assert all("Passphrase:" not in argument for call in calls for argument in call)
    assert all(
        payload is None or payload == (PASSPHRASE + "\n").encode()
        for payload in payloads
    )


def test_generated_primary_requires_direct_signing_capabilities() -> None:
    created = date(2025, 3, 1)
    expiry = date(2027, 3, 1)
    valid_shape = _inspection(created, expiry)

    generation_api._validate_generated_components(
        valid_shape, KeyProfile(), require_expiry=True
    )

    direct_encrypting_primary = KeyRecord(
        fingerprint=PRIMARY,
        algorithm="RSA",
        bits=4096,
        capabilities=frozenset(
            {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
        ),
        direct_capabilities=frozenset(
            {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
        ),
        aggregate_capabilities=frozenset(),
        created_on=created,
        expires_on=expiry,
        is_primary=True,
    )
    wrong_shape = KeyInspection(direct_encrypting_primary, valid_shape.subkeys)

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        generation_api._validate_generated_components(
            wrong_shape, KeyProfile(), require_expiry=True
        )

    assert failure.value.code is (
        generation_api.KeyGenerationErrorCode.GENERATED_KEY_INVALID
    )


def test_gpg_runner_sends_passphrase_only_on_stdin_and_redacts_diagnostics(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "expected = next(int(arg.split('=', 1)[1]) for arg in args "
        "if arg.startswith('--expected-input-length='))\n"
        "payload = sys.stdin.buffer.read()\n"
        "if len(payload) != expected: raise SystemExit(29)\n"
        "if '--fail' in args:\n"
        "    sys.stderr.write(payload.decode('utf-8'))\n"
        "    raise SystemExit(2)\n"
        "sys.stdout.write('\\n'.join(args))\n",
        encoding="utf-8",
    )
    fake_gpg.chmod(0o700)

    payload = bytearray((PASSPHRASE + "\n").encode())
    output = generation_api._run_gpg(
        os.fspath(fake_gpg),
        home,
        (
            "--passphrase-fd",
            "0",
            f"--expected-input-length={len(payload)}",
        ),
        passphrase=payload,
        timeout=5.0,
    )
    assert PASSPHRASE.encode() not in output
    assert b"--passphrase-fd\n0" in output

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        generation_api._run_gpg(
            os.fspath(fake_gpg),
            home,
            (
                "--passphrase-fd",
                "0",
                f"--expected-input-length={len(payload)}",
                "--fail",
            ),
            passphrase=payload,
            timeout=5.0,
        )
    assert failure.value.code is generation_api.KeyGenerationErrorCode.GPG_FAILED
    assert PASSPHRASE not in str(failure.value)
    assert PASSPHRASE not in repr(failure.value)


def test_secret_resolution_failure_is_redacted_before_gpg_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "resolver-output-must-not-escape"

    class FailingResolver(SecretResolver):
        def resolve(self, reference: SecretReference) -> str:
            raise RuntimeError(f"provider diagnostic: {secret}")

    def must_not_run(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("GPG must not run when secret resolution fails")

    monkeypatch.setattr(generation_api, "_run_gpg", must_not_run)
    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _generate(tmp_path / "isolated-home", FailingResolver())

    assert failure.value.code is generation_api.KeyGenerationErrorCode.SECRET_RESOLUTION_FAILED
    assert secret not in str(failure.value)
    assert secret not in repr(failure.value)

    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None

def test_invalid_passphrase_is_not_retained_in_traceback_locals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rejected = "rejected-passphrase-" + ("x" * generation_api._MAX_PASSPHRASE_BYTES)

    def must_not_run(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("GPG must not run for an invalid passphrase")

    monkeypatch.setattr(generation_api, "_run_gpg", must_not_run)
    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _generate(tmp_path / "invalid-passphrase-home", _Resolver(rejected))

    assert failure.value.code is generation_api.KeyGenerationErrorCode.INVALID_PASSPHRASE
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    frame = failure.value.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_filename.endswith("/openpgp/generation.py"):
            assert "secret" not in frame.tb_frame.f_locals
            assert "secret_resolver" not in frame.tb_frame.f_locals
            assert rejected not in repr(frame.tb_frame.f_locals)
        frame = frame.tb_next


def test_gpg_failure_still_stops_the_isolated_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "failed-gpg-home"
    stopped_homes: list[Path] = []

    def fail_generation(*_args: object, **_kwargs: object) -> bytes:
        raise generation_api.KeyGenerationError(
            generation_api.KeyGenerationErrorCode.GPG_FAILED
        )

    monkeypatch.setattr(generation_api, "_run_gpg", fail_generation)
    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stopped_homes.append)

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _generate(home, _Resolver())

    assert failure.value.code is generation_api.KeyGenerationErrorCode.GPG_FAILED
    assert stopped_homes == [home]


def test_agent_shutdown_command_targets_only_the_isolated_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "isolated-gpg-agent-home"
    home.mkdir()
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(
        command: tuple[str, ...], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(generation_api.subprocess, "run", fake_run)

    generation_api._stop_gpg_agent(home)

    assert len(calls) == 1
    command, options = calls[0]
    assert command == ("gpgconf", "--homedir", str(home), "--kill", "gpg-agent")
    environment = options["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {
        "PATH",
        "HOME",
        "GNUPGHOME",
        "LC_ALL",
        "LANG",
        "TZ",
        "TMPDIR",
    }
    assert environment["GNUPGHOME"] == str(home)
    assert environment["HOME"] == str(home)


@pytest.mark.parametrize(
    ("field", "spoof"),
    [
        ("name", "\u0085"),
        ("name", "\u202e"),
        ("comment", "\u2066"),
        ("comment", "\u2028"),
        ("email", "\u2029"),
    ],
)
def test_identity_rejects_unicode_controls_and_format_spoofing(
    field: str, spoof: str
) -> None:
    name = "Trusted publisher"
    email = "trusted@example.test"
    comment: str | None = None
    if field == "name":
        name = f"Trusted{spoof} publisher"
    elif field == "comment":
        comment = f"public{spoof}comment"
    else:
        email = f"trusted{spoof}@example.test"

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        generation_api.KeyIdentity(name=name, email=email, comment=comment)
    assert failure.value.code is generation_api.KeyGenerationErrorCode.INVALID_IDENTITY


def test_invalid_profile_identity_and_destination_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(generation_api.KeyGenerationError) as identity_error:
        generation_api.KeyIdentity("Injected\n%commit")
    assert identity_error.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_IDENTITY
    )
    with pytest.raises(generation_api.KeyGenerationError) as unicode_error:
        generation_api.KeyIdentity("invalid\ud800")
    assert unicode_error.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_IDENTITY
    )

    resolver = _Resolver()
    with pytest.raises(generation_api.KeyGenerationError) as profile_error:
        generation_api.generate_key(
            identity=generation_api.KeyIdentity("Synthetic user"),
            profile=KeyProfile(bits=2048),
            passphrase_reference=REFERENCE,
            secret_resolver=resolver,
            keyring_home=tmp_path / "wrong-profile-home",
        )
    assert profile_error.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_PROFILE
    )
    assert resolver.calls == 0

    occupied = tmp_path / "occupied-home"
    occupied.mkdir()
    (occupied / "existing.data").write_text("do not read", encoding="utf-8")
    with pytest.raises(generation_api.KeyGenerationError) as occupied_error:
        _generate(occupied, resolver)
    assert occupied_error.value.code is (
        generation_api.KeyGenerationErrorCode.KEYRING_NOT_EMPTY
    )

    target = tmp_path / "symlink-target"
    target.mkdir()
    link = tmp_path / "symlink-home"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(generation_api.KeyGenerationError) as symlink_error:
        _generate(link, resolver)
    assert symlink_error.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
    )
    ambient = tmp_path / "ambient-home"
    ambient.mkdir()
    (ambient / "ambient-keyring.data").write_text("untouched", encoding="utf-8")
    monkeypatch.setenv("GNUPGHOME", str(ambient))
    with pytest.raises(generation_api.KeyGenerationError) as ambient_error:
        _generate(ambient, resolver)
    assert ambient_error.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
    )
    assert (ambient / "ambient-keyring.data").read_text(encoding="utf-8") == "untouched"
    assert resolver.calls == 0


def test_key_generation_rejects_replaceable_or_symlinked_parent(
    tmp_path: Path,
) -> None:
    resolver = _Resolver()
    replaceable = tmp_path / "replaceable"
    replaceable.mkdir()
    replaceable.chmod(0o777)
    try:
        with pytest.raises(generation_api.KeyGenerationError) as unsafe:
            _generate(replaceable / "new-home", resolver)
        assert unsafe.value.code is (
            generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
        )
        assert not (replaceable / "new-home").exists()
    finally:
        replaceable.chmod(0o700)

    linked = tmp_path / "linked-parent"
    linked.symlink_to(replaceable, target_is_directory=True)
    with pytest.raises(generation_api.KeyGenerationError) as symlink:
        _generate(linked / "new-home", resolver)
    assert symlink.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
    )
    assert resolver.calls == 0


def test_unconforming_generated_key_is_not_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created = date(2025, 3, 1)
    valid_shape = _inspection(created, None)
    invalid_inspection = KeyInspection(
        primary=KeyRecord(
            fingerprint=PRIMARY,
            algorithm="RSA",
            bits=2048,
            capabilities=valid_shape.primary.capabilities,
            created_on=created,
            expires_on=None,
            is_primary=True,
        ),
        subkeys=valid_shape.subkeys,
    )
    stopped_homes: list[Path] = []
    calls: list[tuple[str, ...]] = []

    def run_gpg(
        _executable: str,
        _home: Path,
        arguments: tuple[str, ...],
        *,
        passphrase: bytearray | None = None,
        timeout: float,
    ) -> bytes:
        del passphrase, timeout
        calls.append(arguments)
        return CERTIFICATE if "--export" in arguments else b""


    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stopped_homes.append)

    def inspect_public_certificate(
        *, certificate: bytes, gpg_binary: str
    ) -> KeyInspection:
        assert certificate == CERTIFICATE
        assert gpg_binary == "fake-gpg"
        return invalid_inspection

    monkeypatch.setattr(generation_api, "_run_gpg", run_gpg)
    monkeypatch.setattr(generation_api, "inspect_key", inspect_public_certificate)
    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _generate(tmp_path / "invalid-generated-home", _Resolver())

    assert failure.value.code is (
        generation_api.KeyGenerationErrorCode.GENERATED_KEY_INVALID
    )
    assert not any("--quick-set-expire" in call for call in calls)
    assert stopped_homes == [tmp_path / "invalid-generated-home"]

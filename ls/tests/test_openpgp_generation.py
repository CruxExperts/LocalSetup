from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from ls.core.openpgp import KeyCapability, KeyProfile
from ls.core.openpgp import generation as generation_api
from ls.core.openpgp.keys import (
    KeyInspection,
    KeyInspectionError,
    KeyInspectionErrorCode,
    KeyRecord,
)
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
        before_launch=None,
    ) -> bytes:
        del timeout
        if before_launch is not None:
            before_launch()
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


def _generate(
    home: Path,
    resolver: SecretResolver,
    *,
    certification_notations: tuple[tuple[str, str], ...] = (),
) -> generation_api.GeneratedKey:
    return generation_api.generate_key(
        identity=generation_api.KeyIdentity(
            name="Synthetic fixture user",
            email="fixture@example.invalid",
            comment="isolated test",
            certification_notations=certification_notations,
        ),
        profile=KeyProfile(),
        passphrase_reference=REFERENCE,
        secret_resolver=resolver,
        keyring_home=home,
        gpg_binary="fake-gpg",
    )


def test_certification_notations_are_present_on_every_generation_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created = date(2028, 2, 29)
    expiry = date(2030, 2, 28)
    notation = ("description@localsetup.example.com", "fixture 100% © metadata")
    calls, _, _ = _configure_generation(
        monkeypatch, created=created, expires=expiry
    )

    _generate(
        tmp_path / "isolated-gpg-home",
        _Resolver(),
        certification_notations=(notation,),
    )

    protected_calls = [
        call
        for call in calls
        if "--generate-key" in call or "--quick-set-expire" in call
    ]
    assert len(protected_calls) == 3
    for call in protected_calls:
        assert "--utf8-strings" in call
        assert call.count("--cert-notation") == 1
        option = call.index("--cert-notation")
        assert call[option + 1] == f"{notation[0]}={notation[1].replace('%', '%%')}"


@pytest.mark.parametrize(
    "notations",
    [
        [],
        (("missing-namespace", "value"),),
        (("description@bad_domain.example", "value"),),
        (("description@localsetup.example.com", "line\n%commit"),),
        (("description@localsetup.example.com", "bidi\u202evalue"),),
        (("description@localsetup.example.com", "invalid\ud800value"),),
        (("n" * 1_000_000 + "@localsetup.example.com", "value"),),
        (("description@localsetup.example.com", "x" * 1_000_000),),
        (
            ("description@localsetup.example.com", "one"),
            ("DESCRIPTION@LOCALSETUP.EXAMPLE.COM", "two"),
        ),
        (("description@localsetup.example.com", "x" * 4097),),
        tuple((f"notation{index}@localsetup.example.com", "x") for index in range(17)),
        tuple(
            (f"notation{index}@localsetup.example.com", "x" * 4000)
            for index in range(5)
        ),
    ],
)
def test_identity_rejects_invalid_certification_notations(notations: object) -> None:
    with pytest.raises(generation_api.KeyGenerationError) as failure:
        generation_api.KeyIdentity(
            "Synthetic user", certification_notations=notations  # type: ignore[arg-type]
        )
    assert failure.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_CERTIFICATION_NOTATION
    )


def test_identity_keeps_certification_notations_immutable_and_optional() -> None:
    legacy = generation_api.KeyIdentity("Legacy fixture", "user@example.invalid")
    notations = (("description@localsetup.example.com", "signed metadata"),)
    extended = generation_api.KeyIdentity(
        "Legacy fixture",
        "user@example.invalid",
        certification_notations=notations,
    )

    assert legacy.certification_notations == ()
    assert extended.certification_notations == notations



def test_generation_candidate_skips_gpg_for_missing_and_empty_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("empty candidate inspection must not invoke GnuPG")

    monkeypatch.setattr(generation_api, "_run_gpg", must_not_run)
    missing_home = tmp_path / "missing-candidate-home"
    assert generation_api.inspect_generation_candidate(
        keyring_home=missing_home, fingerprint=PRIMARY
    ) is None
    assert not missing_home.exists()

    empty_home = tmp_path / "empty-candidate-home"
    empty_home.mkdir(mode=0o700)
    empty_home.chmod(0o700)
    before = empty_home.stat()

    assert generation_api.inspect_generation_candidate(
        keyring_home=empty_home, fingerprint=PRIMARY
    ) is None

    after = empty_home.stat()
    assert list(empty_home.iterdir()) == []
    assert (before.st_dev, before.st_ino, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
    )



def test_generation_candidate_rejects_unsafe_homes_without_changing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("unsafe candidate inspection must not invoke GnuPG")

    monkeypatch.setattr(generation_api, "_run_gpg", must_not_run)
    unsafe_home = tmp_path / "unsafe-candidate-home"
    unsafe_home.mkdir(mode=0o700)
    unsafe_home.chmod(0o750)
    before = unsafe_home.stat()

    with pytest.raises(generation_api.KeyGenerationError) as unsafe_root:
        generation_api.inspect_generation_candidate(
            keyring_home=unsafe_home, fingerprint=PRIMARY
        )
    assert unsafe_root.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
    )
    after = unsafe_home.stat()
    assert stat.S_IMODE(after.st_mode) == 0o750
    assert (before.st_dev, before.st_ino, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
    )

    linked_home = tmp_path / "linked-candidate-home"
    linked_home.mkdir(mode=0o700)
    linked_home.chmod(0o700)
    target = linked_home / "real-public-ring"
    target.write_bytes(b"synthetic public candidate ring")
    target.chmod(0o600)
    link = linked_home / "pubring.kbx"
    link.symlink_to(target.name)
    link_identity = (link.lstat().st_dev, link.lstat().st_ino)

    with pytest.raises(generation_api.KeyGenerationError) as unsafe_entry:
        generation_api.inspect_generation_candidate(
            keyring_home=linked_home, fingerprint=PRIMARY
        )
    assert unsafe_entry.value.code is (
        generation_api.KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
    )
    assert link.is_symlink()
    assert (link.lstat().st_dev, link.lstat().st_ino) == link_identity
    assert target.read_bytes() == b"synthetic public candidate ring"

def test_generation_candidate_requires_exact_selection_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "candidate-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    ring = home / "pubring.kbx"
    ring.write_bytes(b"synthetic public candidate ring")
    ring.chmod(0o600)
    expected = generation_api.KeyIdentity(
        name="Synthetic fixture user",
        email="fixture@example.invalid",
        comment="isolated test",
        certification_notations=(("fixture@localsetup.example.com", "value"),),
    )
    calls: list[tuple[str, ...]] = []
    validations: list[tuple[str, generation_api.KeyIdentity]] = []

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
        return CERTIFICATE

    def inspect_public(*, certificate: bytes, gpg_binary: str) -> KeyInspection:
        assert certificate == CERTIFICATE
        assert gpg_binary == "fake-gpg"
        today = date.today()
        return _inspection(today, KeyProfile().expiry_date(today))

    def validate_identity(
        _executable: str,
        _home: Path,
        _certificate: bytes,
        fingerprint: str,
        identity: generation_api.KeyIdentity,
    ) -> None:
        validations.append((fingerprint, identity))

    monkeypatch.setattr(generation_api, "_run_gpg", run_gpg)
    monkeypatch.setattr(generation_api, "inspect_key", inspect_public)
    monkeypatch.setattr(
        generation_api, "_validate_candidate_identity", validate_identity
    )

    unknown_fingerprint = generation_api.inspect_generation_candidate(
        keyring_home=home,
        expected_identity=expected,
        gpg_binary="fake-gpg",
    )
    assert unknown_fingerprint is not None
    assert unknown_fingerprint.primary_fingerprint == PRIMARY
    assert calls[-1] == ("--armor", "--export")
    assert validations[-1] == (PRIMARY, expected)

    selected_fingerprint = generation_api.inspect_generation_candidate(
        keyring_home=home,
        fingerprint=PRIMARY,
        expected_identity=expected,
        gpg_binary="fake-gpg",
    )
    assert selected_fingerprint is not None
    assert selected_fingerprint.primary_fingerprint == PRIMARY
    assert calls[-1] == ("--armor", "--export", PRIMARY)

    with pytest.raises(generation_api.KeyGenerationError) as wrong_selection:
        generation_api.inspect_generation_candidate(
            keyring_home=home,
            fingerprint="C" * 40,
            gpg_binary="fake-gpg",
        )
    assert wrong_selection.value.code is (
        generation_api.KeyGenerationErrorCode.CANDIDATE_NOT_FOUND
    )

    with pytest.raises(generation_api.KeyGenerationError) as missing_identity:
        generation_api.inspect_generation_candidate(
            keyring_home=home, gpg_binary="fake-gpg"
        )
    assert missing_identity.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_CANDIDATE_SELECTION
    )


def test_generation_candidate_preserves_ambiguous_and_incomplete_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "candidate-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    ring = home / "pubring.kbx"
    original = b"synthetic public candidate ring"
    ring.write_bytes(original)
    ring.chmod(0o600)
    expected = generation_api.KeyIdentity(
        name="Synthetic fixture user", email="fixture@example.invalid"
    )

    monkeypatch.setattr(generation_api, "_run_gpg", lambda *_args, **_kwargs: CERTIFICATE)

    def ambiguous_inspection(**_kwargs):
        raise KeyInspectionError(KeyInspectionErrorCode.AMBIGUOUS_KEY)

    monkeypatch.setattr(generation_api, "inspect_key", ambiguous_inspection)
    with pytest.raises(generation_api.KeyGenerationError) as ambiguous:
        generation_api.inspect_generation_candidate(
            keyring_home=home,
            expected_identity=expected,
            gpg_binary="fake-gpg",
        )
    assert ambiguous.value.code is (
        generation_api.KeyGenerationErrorCode.CANDIDATE_AMBIGUOUS
    )
    assert ring.read_bytes() == original
    assert sorted(entry.name for entry in home.iterdir()) == ["pubring.kbx"]

    monkeypatch.setattr(generation_api, "_run_gpg", lambda *_args, **_kwargs: b"")
    with pytest.raises(generation_api.KeyGenerationError) as incomplete:
        generation_api.inspect_generation_candidate(
            keyring_home=home,
            expected_identity=expected,
            gpg_binary="fake-gpg",
        )
    assert incomplete.value.code is (
        generation_api.KeyGenerationErrorCode.CANDIDATE_INVALID
    )
    assert ring.read_bytes() == original
    assert sorted(entry.name for entry in home.iterdir()) == ["pubring.kbx"]

@pytest.mark.parametrize("change", ["content", "home", "ancestor"])
def test_candidate_rejects_path_or_persistent_changes_during_inspection(tmp_path, monkeypatch, change):
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    home = parent / "candidate"
    home.mkdir(mode=0o700)
    ring = home / "pubring.kbx"
    ring.write_bytes(b"original fixture")
    ring.chmod(0o600)

    def export(*args, **kwargs):
        if change == "content":
            ring.write_bytes(b"changed persistent fixture")
        elif change == "home":
            home.rename(parent / "retained")
            home.mkdir(mode=0o700)
            ring.write_bytes(b"replacement fixture")
            ring.chmod(0o600)
        else:
            retained = tmp_path / "retained-parent"
            parent.rename(retained)
            parent.symlink_to(retained, target_is_directory=True)
        return CERTIFICATE

    today = date.today()
    monkeypatch.setattr(generation_api, "_run_gpg", export)
    monkeypatch.setattr(generation_api, "inspect_key",
                        lambda **kwargs: _inspection(today, KeyProfile().expiry_date(today)))
    with pytest.raises(generation_api.KeyGenerationError) as denied:
        generation_api.inspect_generation_candidate(
            keyring_home=home, fingerprint=PRIMARY, gpg_binary="fake-gpg",
        )
    assert denied.value.code is generation_api.KeyGenerationErrorCode.CANDIDATE_UNAVAILABLE
    if change == "home":
        assert (parent / "retained" / "pubring.kbx").read_bytes() == b"original fixture"
        assert ring.read_bytes() == b"replacement fixture"
    elif change == "content":
        assert ring.read_bytes() == b"changed persistent fixture"
    else:
        assert parent.is_symlink()
        assert ring.read_bytes() == b"original fixture"


def _colon_record(fields: list[bytes]) -> bytes:
    return b":".join(fields) + b":"


def _colon_uid(identity: generation_api.KeyIdentity) -> bytes:
    value = generation_api._expected_user_id(identity).encode("utf-8")
    escaped = bytearray()
    for character in value:
        if character == ord(":"):
            escaped.extend(b"\\x3a")
        elif character == ord("\\"):
            escaped.extend(b"\\\\")
        else:
            escaped.append(character)
    fields = [b"uid", b"-"] + [b""] * 7 + [bytes(escaped)]
    return _colon_record(fields)


def _colon_signature(
    *,
    fingerprint: str,
    validity: bytes = b"!",
    signature_class: bytes = b"13x",
) -> bytes:
    fields = [b""] * 13
    fields[0] = b"sig"
    fields[1] = validity
    fields[10] = signature_class
    fields[12] = fingerprint.encode("ascii")
    return _colon_record(fields)


def _colon_notation(
    name: str,
    value: str,
    *,
    hashed: bool = True,
) -> bytes:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    data = (
        b"\x80\x00\x00\x00"
        + len(name_bytes).to_bytes(2, "big")
        + len(value_bytes).to_bytes(2, "big")
        + name_bytes
        + value_bytes
    )
    escaped = bytearray()
    for character in data:
        if character < 0x20 or character >= 0x7F or character in {0x25, 0x3A}:
            escaped.extend(f"%{character:02X}".encode("ascii"))
        else:
            escaped.append(character)
    flags = b"1" if hashed else b"0"
    return _colon_record([b"spk", b"20", flags, str(len(data)).encode(), bytes(escaped)])


def _candidate_colon_listing(
    identity: generation_api.KeyIdentity,
    signatures: tuple[tuple[bytes, tuple[bytes, ...]], ...],
) -> bytes:
    primary_fields = [b"pub"] + [b""] * 9
    primary_fingerprint = [b"fpr"] + [b""] * 8 + [PRIMARY.encode("ascii")]
    records = [
        _colon_record(primary_fields),
        _colon_record(primary_fingerprint),
        _colon_uid(identity),
    ]
    for signature, subpackets in signatures:
        records.append(signature)
        records.extend(subpackets)
    return b"\n".join(records) + b"\n"


def _validate_colon_listing(
    monkeypatch: pytest.MonkeyPatch,
    identity: generation_api.KeyIdentity,
    listing: bytes,
) -> tuple[tuple[str, ...], ...]:
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
        return listing

    monkeypatch.setattr(generation_api, "_run_gpg", run_gpg)
    generation_api._validate_candidate_identity(
        "fake-gpg",
        Path("isolated-test-home"),
        b"synthetic certificate",
        PRIMARY,
        identity,
    )
    return tuple(calls)


def test_candidate_identity_binds_uid_signature_issuer_and_hashed_notations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = generation_api.KeyIdentity(
        name="Synthetic: \\unicode λ",
        email="fixture@example.invalid",
        certification_notations=(("fixture@localsetup.example.com", "signed value"),),
    )
    listing = _candidate_colon_listing(
        identity,
        (
            (
                _colon_signature(fingerprint=PRIMARY),
                (_colon_notation("fixture@localsetup.example.com", "signed value"),),
            ),
        ),
    )

    calls = _validate_colon_listing(monkeypatch, identity, listing)

    assert len(calls) == 1
    arguments = calls[0]
    assert "--no-sig-cache" in arguments
    assert "--with-fingerprint" in arguments
    assert "--check-sigs" in arguments
    assert "show-sig-subpackets" in arguments
    assert arguments[-1] == PRIMARY


def test_candidate_identity_rejects_unhashed_expected_notation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = generation_api.KeyIdentity(
        "Synthetic user",
        certification_notations=(("fixture@localsetup.example.com", "expected"),),
    )
    listing = _candidate_colon_listing(
        identity,
        (
            (
                _colon_signature(fingerprint=PRIMARY),
                (
                    _colon_notation(
                        "fixture@localsetup.example.com", "expected", hashed=False
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _validate_colon_listing(monkeypatch, identity, listing)
    assert failure.value.code is generation_api.KeyGenerationErrorCode.CANDIDATE_INVALID


def test_candidate_identity_rejects_invalid_expected_selfcert_and_valid_unrelated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = generation_api.KeyIdentity(
        "Synthetic user",
        certification_notations=(("fixture@localsetup.example.com", "expected"),),
    )
    listing = _candidate_colon_listing(
        identity,
        (
            (
                _colon_signature(fingerprint=PRIMARY, validity=b"-"),
                (_colon_notation("fixture@localsetup.example.com", "expected"),),
            ),
            (
                _colon_signature(fingerprint=PRIMARY),
                (_colon_notation("other@localsetup.example.com", "unrelated"),),
            ),
        ),
    )

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _validate_colon_listing(monkeypatch, identity, listing)
    assert failure.value.code is generation_api.KeyGenerationErrorCode.CANDIDATE_INVALID


def test_candidate_identity_does_not_match_fingerprint_inside_notation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = generation_api.KeyIdentity(
        "Synthetic user",
        certification_notations=(("fixture@localsetup.example.com", PRIMARY),),
    )
    listing = _candidate_colon_listing(
        identity,
        (
            (
                _colon_signature(fingerprint=ENCRYPTION_SUBKEY),
                (_colon_notation("fixture@localsetup.example.com", PRIMARY),),
            ),
        ),
    )

    with pytest.raises(generation_api.KeyGenerationError) as failure:
        _validate_colon_listing(monkeypatch, identity, listing)
    assert failure.value.code is generation_api.KeyGenerationErrorCode.CANDIDATE_INVALID



def test_final_generated_certificate_keeps_signed_unicode_and_literal_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which("gpg")
    if executable is None:
        pytest.skip("GnuPG is unavailable")

    home = tmp_path / "isolated-real-gpg-home"
    assert not home.exists()
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    metadata = home.lstat()
    owned_identity = (metadata.st_dev, metadata.st_ino)
    stop_agent = generation_api._stop_gpg_agent
    stopped_homes: list[Path] = []

    def stop_owned_agent(selected_home: Path) -> None:
        stopped_homes.append(selected_home)
        stop_agent(selected_home)

    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stop_owned_agent)
    try:
        _assert_final_generated_certificate(home, executable)
        assert stopped_homes == [home]
    finally:
        _remove_owned_fixture_home(home, owned_identity)


def _assert_final_generated_certificate(home: Path, executable: str) -> None:

    year = datetime.now().year
    rights = (
        "Private asset. Property of Crux Experts LLC. Use, reproduction, "
        "modification, distribution, or publication without prior written permission is prohibited."
    )
    description = " | ".join(
        (
            "https://github.com/CruxExperts/LocalSetup",
            f"Copyright © {year} Crux Experts LLC",
            "https://www.cruxexperts.com/",
            rights,
        )
    )
    notations = (
        ("description@localsetup.cruxexperts.com", description),
        ("literal-percent@localsetup.cruxexperts.com", "%f %p %k %% %U"),
    )
    identity = generation_api.KeyIdentity(
        name="LocalSetup Encryption — machine-7f3a9c2d: \\fixture",
        email="pgp@cruxexperts.com",
        certification_notations=notations,
    )
    generated = generation_api.generate_key(
        identity=identity,
        profile=KeyProfile(),
        passphrase_reference=REFERENCE,
        secret_resolver=SecretResolver(env_values={REFERENCE.name: PASSPHRASE}),
        keyring_home=home,
        gpg_binary=executable,
    )

    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment.update({"GNUPGHOME": str(home), "LC_ALL": "C"})
    inspected = subprocess.run(
        [
            executable,
            "--no-options",
            "--no-autostart",
            "--homedir",
            str(home),
            "--list-packets",
            "--verbose",
        ],
        input=generated.public_certificate,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        close_fds=True,
        env=environment,
    )
    assert inspected.returncode == 0
    packet_text = (inspected.stdout + inspected.stderr).decode("utf-8", "replace")
    assert "hashed subpkt 20" in packet_text
    assert "LocalSetup Encryption" in packet_text
    assert "machine-7f3a9c2d" in packet_text
    assert "pgp@cruxexperts.com" in packet_text
    for name, value in notations:
        assert name in packet_text
        assert value in packet_text

    signatures = subprocess.run(
        [
            executable,
            "--no-options",
            "--no-autostart",
            "--homedir",
            str(home),
            "--with-colons",
            "--check-sigs",
            generated.primary_fingerprint,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        close_fds=True,
        env=environment,
    )
    assert signatures.returncode == 0
    self_signatures = [
        line.split(":")
        for line in signatures.stdout.decode("utf-8", "replace").splitlines()
        if line.startswith("sig:")
    ]
    assert any(
        len(fields) > 10
        and fields[1] == "!"
        and fields[10][:2].lower() == "13"
        for fields in self_signatures
    )

    candidate_home = home.parent / "isolated-public-only-candidate"
    candidate_home.mkdir(mode=0o700)
    candidate_home.chmod(0o700)
    candidate_metadata = candidate_home.lstat()
    candidate_identity = (candidate_metadata.st_dev, candidate_metadata.st_ino)
    candidate_environment = dict(environment)
    candidate_environment.update(
        {
            "GNUPGHOME": str(candidate_home),
            "HOME": str(candidate_home),
            "TMPDIR": str(candidate_home),
        }
    )
    try:
        imported = subprocess.run(
            [
                executable,
                "--no-options",
                "--no-autostart",
                "--homedir",
                str(candidate_home),
                "--import",
            ],
            input=generated.public_certificate,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            close_fds=True,
            env=candidate_environment,
        )
        assert imported.returncode == 0
        assert not (candidate_home / "private-keys-v1.d").exists()
        for entry in candidate_home.rglob("*"):
            if entry.is_dir():
                entry.chmod(0o700)
            elif entry.is_file():
                entry.chmod(0o600)

        before_inspection = tuple(
            sorted(
                (
                    entry.name,
                    entry.stat().st_mode,
                    entry.stat().st_size,
                    entry.stat().st_mtime_ns,
                )
                for entry in candidate_home.iterdir()
            )
        )
        known = generation_api.inspect_generation_candidate(
            keyring_home=candidate_home,
            fingerprint=generated.primary_fingerprint,
            expected_identity=identity,
            profile=KeyProfile(),
            gpg_binary=executable,
        )
        by_identity = generation_api.inspect_generation_candidate(
            keyring_home=candidate_home,
            expected_identity=identity,
            profile=KeyProfile(),
            gpg_binary=executable,
        )
        after_inspection = tuple(
            sorted(
                (
                    entry.name,
                    entry.stat().st_mode,
                    entry.stat().st_size,
                    entry.stat().st_mtime_ns,
                )
                for entry in candidate_home.iterdir()
            )
        )
        assert known is not None and by_identity is not None
        assert known.primary_fingerprint == generated.primary_fingerprint
        assert by_identity.primary_fingerprint == generated.primary_fingerprint
        assert known.subkey_fingerprints == generated.subkey_fingerprints
        assert by_identity.subkey_fingerprints == generated.subkey_fingerprints
        assert before_inspection == after_inspection
    finally:
        _remove_owned_fixture_home(candidate_home, candidate_identity)


def _remove_owned_fixture_home(
    home: Path, expected_identity: tuple[int, int]
) -> None:
    try:
        metadata = home.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        raise AssertionError("disposable GnuPG fixture home identity changed")
    shutil.rmtree(home)
    if home.exists() or home.is_symlink():
        raise AssertionError("disposable GnuPG fixture home cleanup failed")


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


def test_key_generation_does_not_follow_leaf_created_during_atomic_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "new-home"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    original_mkdir = Path.mkdir

    def race_mkdir(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path == destination:
            path.symlink_to(unrelated, target_is_directory=True)
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)
    resolver = _Resolver()
    with pytest.raises(generation_api.KeyGenerationError) as raced:
        _generate(destination, resolver)
    assert raced.value.code is (
        generation_api.KeyGenerationErrorCode.INVALID_KEYRING_HOME
    )
    assert not any(unrelated.iterdir())
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


def _native_generation_input(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: generation_api.KeyIdentity,
    profile: KeyProfile,
    home: Path,
    expected_home: Path | None = None,
    on_request=None,
):
    expected = generation_api.describe_generation_operation(
        identity=identity,
        profile=profile,
        keyring_home=expected_home if expected_home is not None else home,
    )

    class FakeNativeInput:
        def __init__(self):
            self.lease = None
            self.requests = 0
            self.finished: list[object] = []
            self.buffer: bytearray | None = None
            self.current = True

        def claim(self, operation):
            if operation != expected:
                raise RuntimeError("native operation mismatch")
            if self.lease is not None:
                raise RuntimeError("native context already claimed")
            self.lease = object()
            return self.lease

        def request_passphrase(self, operation):
            assert operation == expected
            self.requests += 1
            self.buffer = bytearray(PASSPHRASE.encode("utf-8"))
            try:
                if on_request is not None:
                    on_request(self)
            except BaseException:
                self.buffer[:] = b"\x00" * len(self.buffer)
                raise
            return self.buffer

        def checkpoint(self, operation):
            assert operation == expected
            if not self.current:
                raise RuntimeError("native owner is no longer current")

        def finish(self, lease):
            assert lease is self.lease
            self.finished.append(lease)
            self.lease = None

    native_input = FakeNativeInput()
    monkeypatch.setattr(generation_api, "NativeInputContext", FakeNativeInput)
    return native_input, expected


def _install_native_resolver(
    monkeypatch: pytest.MonkeyPatch,
    resolver: SecretResolver,
) -> None:
    monkeypatch.setattr(
        resolver,
        "resolve_buffer",
        lambda reference, *, operation, native_input: native_input.request_passphrase(operation),
    )


@pytest.mark.parametrize("failure", ["claim_mismatch", "cancelled", "owner_loss"])
def test_native_generation_failures_before_preparation_leave_home_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    identity = generation_api.KeyIdentity("Synthetic native generation")
    profile = KeyProfile()
    home = tmp_path / "native-keyring-home"
    if failure == "owner_loss":
        home.mkdir(mode=0o700)
        home.chmod(0o755)
    initial = None if not home.exists() else (
        home.stat().st_dev, home.stat().st_ino, home.stat().st_mode,
    )

    def lose_owner(native_input) -> None:
        if failure == "cancelled":
            raise RuntimeError("dialog cancelled")
        if failure == "owner_loss":
            native_input.current = False

    native_input, _expected = _native_generation_input(
        monkeypatch,
        identity=identity,
        profile=profile,
        home=home,
        expected_home=(tmp_path / "different-home") if failure == "claim_mismatch" else None,
        on_request=lose_owner if failure != "claim_mismatch" else None,
    )
    resolver = _Resolver()
    _install_native_resolver(monkeypatch, resolver)
    monkeypatch.setattr(
        generation_api, "_run_gpg",
        lambda *_args, **_kwargs: pytest.fail("GnuPG must not run before accepted input"),
    )

    with pytest.raises(generation_api.KeyGenerationError):
        generation_api.generate_key(
            identity=identity, profile=profile,
            passphrase_reference=None, secret_resolver=resolver,
            keyring_home=home, gpg_binary="fake-gpg", native_input=native_input,
        )

    assert native_input.requests == (0 if failure == "claim_mismatch" else 1)
    assert len(native_input.finished) == (0 if failure == "claim_mismatch" else 1)
    if native_input.buffer is not None:
        assert not any(native_input.buffer)
    if initial is None:
        assert not home.exists()
    else:
        after = home.stat()
        assert (after.st_dev, after.st_ino, after.st_mode) == initial


def test_native_generation_prepares_mode_755_leaf_after_input_and_reuses_owned_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = date(2028, 2, 29)
    expires = KeyProfile().expiry_date(created)
    _calls, payloads, _legacy_stops = _configure_generation(
        monkeypatch, created=created, expires=expires,
    )
    identity = generation_api.KeyIdentity(
        "Synthetic native user", email="native@example.invalid",
    )
    profile = KeyProfile()
    home = tmp_path / "native-permissive-home"
    home.mkdir(mode=0o700)
    home.chmod(0o755)
    initial_identity = (home.stat().st_dev, home.stat().st_ino)
    native_input, _expected = _native_generation_input(
        monkeypatch, identity=identity, profile=profile, home=home,
    )
    resolver = _Resolver()
    _install_native_resolver(monkeypatch, resolver)
    passphrase_ids: list[int] = []
    fake_run = generation_api._run_gpg

    def track_passphrase(*args, **kwargs):
        passphrase = kwargs.get("passphrase")
        if passphrase is not None:
            passphrase_ids.append(id(passphrase))
        return fake_run(*args, **kwargs)

    monkeypatch.setattr(generation_api, "_run_gpg", track_passphrase)
    stopped: list[Path] = []

    def stop_agent(selected_home: Path, *, before_launch=None) -> None:
        if before_launch is not None:
            before_launch()
        stopped.append(selected_home)

    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stop_agent)

    generated = generation_api.generate_key(
        identity=identity, profile=profile,
        passphrase_reference=None, secret_resolver=resolver,
        keyring_home=home, gpg_binary="fake-gpg", native_input=native_input,
    )

    assert generated.primary_fingerprint == PRIMARY
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert (home.stat().st_dev, home.stat().st_ino) == initial_identity
    assert not (home / ".localsetup-keygen.batch").exists()
    assert stopped == [home]
    assert native_input.requests == 1 and len(native_input.finished) == 1
    assert passphrase_ids and len(set(passphrase_ids)) == 1
    assert all(value == PASSPHRASE.encode("utf-8") + b"\n" for value in payloads if value is not None)
    assert not any(native_input.buffer)


def test_native_generation_owner_loss_after_launch_stops_only_prepared_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = date(2028, 2, 29)
    expires = KeyProfile().expiry_date(created)
    _calls, _payloads, _legacy_stops = _configure_generation(
        monkeypatch, created=created, expires=expires,
    )
    identity = generation_api.KeyIdentity("Synthetic revoked generation")
    profile = KeyProfile()
    home = tmp_path / "revoked-generated-home"
    native_input, _expected = _native_generation_input(
        monkeypatch, identity=identity, profile=profile, home=home,
    )
    resolver = _Resolver()
    _install_native_resolver(monkeypatch, resolver)
    simulated_run = generation_api._run_gpg

    def revoke_after_generation_launch(*args, **kwargs):
        result = simulated_run(*args, **kwargs)
        if "--generate-key" in args[2]:
            native_input.current = False
        return result

    monkeypatch.setattr(generation_api, "_run_gpg", revoke_after_generation_launch)
    stopped: list[Path] = []

    def stop_owned_agent(selected_home: Path, *, before_launch=None) -> None:
        if before_launch is not None:
            before_launch()
        stopped.append(selected_home)

    monkeypatch.setattr(generation_api, "_stop_gpg_agent", stop_owned_agent)

    with pytest.raises(generation_api.KeyGenerationError):
        generation_api.generate_key(
            identity=identity, profile=profile,
            passphrase_reference=None, secret_resolver=resolver,
            keyring_home=home, gpg_binary="fake-gpg", native_input=native_input,
        )

    assert stopped == [home]
    assert (home / "private-keys-v1.d" / "synthetic-protected-key.fixture").exists()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert not (home / ".localsetup-keygen.batch").exists()
    assert len(native_input.finished) == 1
    assert not any(native_input.buffer)


@pytest.mark.parametrize("change", ["appeared", "mode", "replace", "symlink"])
def test_native_generation_rechecks_leaf_before_preparing_or_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    identity = generation_api.KeyIdentity("Synthetic native race")
    profile = KeyProfile()
    home = tmp_path / "native-selected-home"
    if change == "mode":
        home.mkdir(mode=0o700)
        home.chmod(0o755)
    elif change == "replace":
        home.mkdir(mode=0o700)
        home.chmod(0o755)
    expected_inode = None if not home.exists() else home.stat().st_ino
    target = tmp_path / "native-external-target"
    target.mkdir(mode=0o700)
    target.chmod(0o755)
    marker = target / "leave-alone"
    marker.write_text("external", encoding="utf-8")

    def change_home(_native_input) -> None:
        if change == "appeared":
            home.mkdir(mode=0o700)
            (home / "external-entry").write_text("leave", encoding="utf-8")
        elif change == "mode":
            home.chmod(0o750)
        elif change == "replace":
            home.rename(tmp_path / "old-native-home")
            home.mkdir(mode=0o700)
            home.chmod(0o755)
            (home / "replacement").write_text("leave", encoding="utf-8")
        else:
            home.symlink_to(target, target_is_directory=True)

    native_input, _expected = _native_generation_input(
        monkeypatch, identity=identity, profile=profile, home=home,
        on_request=change_home,
    )
    resolver = _Resolver()
    _install_native_resolver(monkeypatch, resolver)
    monkeypatch.setattr(
        generation_api, "_run_gpg",
        lambda *_args, **_kwargs: pytest.fail("stale generation selection must not launch GnuPG"),
    )

    with pytest.raises(generation_api.KeyGenerationError):
        generation_api.generate_key(
            identity=identity, profile=profile,
            passphrase_reference=None, secret_resolver=resolver,
            keyring_home=home, gpg_binary="fake-gpg", native_input=native_input,
        )

    assert len(native_input.finished) == 1
    assert not any(native_input.buffer)
    assert marker.read_text(encoding="utf-8") == "external"
    if change == "mode":
        assert stat.S_IMODE(home.stat().st_mode) == 0o750
    elif change == "replace":
        assert home.stat().st_ino != expected_inode
        assert (home / "replacement").exists()
    elif change == "symlink":
        assert home.is_symlink() and marker.exists()


def test_native_generation_home_replacement_before_launch_does_not_touch_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = generation_api.KeyIdentity("Synthetic native replacement")
    profile = KeyProfile()
    home = tmp_path / "native-prepared-home"
    moved = tmp_path / "native-prepared-home-moved"
    replacement_marker = "replacement must remain unchanged"
    native_input, _expected = _native_generation_input(
        monkeypatch, identity=identity, profile=profile, home=home,
    )
    resolver = _Resolver()
    _install_native_resolver(monkeypatch, resolver)
    launches: list[tuple[str, ...]] = []

    def replace_before_spawn(
        _executable: str,
        _selected_home: Path,
        arguments: tuple[str, ...],
        *,
        passphrase: bytearray | None = None,
        timeout: float,
        before_launch=None,
    ) -> bytes:
        del passphrase, timeout
        if "--generate-key" in arguments:
            home.rename(moved)
            home.mkdir(mode=0o700)
            home.chmod(0o755)
            (home / "marker").write_text(replacement_marker, encoding="utf-8")
        if before_launch is not None:
            before_launch()
        launches.append(arguments)
        return b""

    monkeypatch.setattr(generation_api, "_run_gpg", replace_before_spawn)
    stopped: list[Path] = []
    monkeypatch.setattr(
        generation_api, "_stop_gpg_agent",
        lambda selected_home, **_kwargs: stopped.append(selected_home),
    )

    with pytest.raises(generation_api.KeyGenerationError):
        generation_api.generate_key(
            identity=identity, profile=profile,
            passphrase_reference=None, secret_resolver=resolver,
            keyring_home=home, gpg_binary="fake-gpg", native_input=native_input,
        )

    assert launches == []
    assert stopped == []
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert (home / "marker").read_text(encoding="utf-8") == replacement_marker
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700
    assert list(moved.iterdir()) == []
    assert len(native_input.finished) == 1
    assert not any(native_input.buffer)

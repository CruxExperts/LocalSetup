from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from ls.core.openpgp import (
    ContractErrorCode,
    KeyCapability,
    KeyInspection,
    KeyInspectionError,
    KeyInspectionErrorCode,
    KeyProfile,
    KeyRecord,
    LocalTrust,
    OpenPGPContractError,
    RSA_4096_TWO_YEAR_PROFILE,
    enroll_key,
    expected_expiry_date,
    inspect_key,
)
from ls.core.openpgp import keys as keys_api


PRIMARY = "A" * 40
ENCRYPTION_SUBKEY = "B" * 40
REVOKED_SUBKEY = "C" * 40
CERTIFICATE = (
    "-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    "Version: synthetic test input\n\n"
    "ZmFrZQ==\n"
    "-----END PGP PUBLIC KEY BLOCK-----\n"
)


def _epoch(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())


def _colon_record(
    kind: str,
    fingerprint: str,
    capabilities: str,
    status: str = "-",
    bits: int = 4096,
) -> str:
    created = datetime.now(timezone.utc).date()
    expires = expected_expiry_date(created)
    fields = [
        kind,
        status,
        str(bits),
        "1",
        fingerprint[-16:],
        str(_epoch(created)),
        str(_epoch(expires)),
        "",
        "",
        "",
        "",
        capabilities,
    ]
    return ":".join(fields) + f":\nfpr:::::::::{fingerprint}:\n"


def _colon_output(
    *,
    subkey_capabilities: str = "e",
    subkey_status: str = "-",
    include_subkey: bool = True,
    add_revoked_subkey: bool = False,
    primary_bits: int = 4096,
    primary_capabilities: str = "SC",
) -> bytes:
    output = _colon_record("pub", PRIMARY, primary_capabilities, bits=primary_bits)
    if include_subkey:
        output += _colon_record(
            "sub", ENCRYPTION_SUBKEY, subkey_capabilities, subkey_status
        )
    if add_revoked_subkey:
        output += _colon_record("sub", REVOKED_SUBKEY, "e", "r")
    return output.encode("utf-8")


def _fake_gpg(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> list[tuple[Path, tuple[str, ...], frozenset[str]]]:
    calls: list[tuple[Path, tuple[str, ...], frozenset[str]]] = []

    def run_gpg(
        _executable: str,
        home: Path,
        arguments: tuple[str, ...],
        *,
        include_stderr: bool = False,
    ) -> bytes:
        calls.append(
            (home, arguments, frozenset(path.name for path in home.iterdir()))
        )
        if "--list-keys" in arguments:
            return output
        return b""

    monkeypatch.setattr(keys_api, "_run_gpg", run_gpg)
    return calls


def _inspect_certificate(monkeypatch: pytest.MonkeyPatch, output: bytes):
    _fake_gpg(monkeypatch, output)
    return inspect_key(certificate=CERTIFICATE)


def test_valid_profile_requires_explicit_pinned_enrollment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(monkeypatch, _colon_output())
    authority = LocalTrust()

    assert inspection.primary_fingerprint == PRIMARY
    assert inspection.primary.algorithm == "RSA"
    assert inspection.primary.bits == 4096
    assert inspection.primary.expires_on == expected_expiry_date(
        inspection.primary.created_on
    )
    assert inspection.signing_fingerprints == (PRIMARY,)
    assert inspection.encryption_subkey_fingerprints == (ENCRYPTION_SUBKEY,)
    assert inspection.capabilities == frozenset(
        {KeyCapability.SIGN, KeyCapability.CERTIFY, KeyCapability.ENCRYPT}
    )
    assert authority.fingerprints == frozenset()

    enrollment = enroll_key(
        inspection,
        expected_fingerprint=PRIMARY,
        required_capabilities={KeyCapability.SIGN, KeyCapability.ENCRYPT},
        profile=RSA_4096_TWO_YEAR_PROFILE,
        local_trust=authority,
    )

    assert enrollment.local_trust.fingerprints == frozenset({PRIMARY})
    assert authority.fingerprints == frozenset()
    assert enrollment.required_capabilities == frozenset(
        {KeyCapability.SIGN, KeyCapability.ENCRYPT}
    )


def test_enrollment_rechecks_exact_expiry_after_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(monkeypatch, _colon_output())
    expires_epoch = inspection.primary.expires_epoch
    assert expires_epoch is not None
    monkeypatch.setattr(keys_api.time, "time", lambda: expires_epoch + 1)

    with pytest.raises(KeyInspectionError) as raised:
        enroll_key(
            inspection,
            expected_fingerprint=PRIMARY,
            required_capabilities={KeyCapability.SIGN},
            profile=RSA_4096_TWO_YEAR_PROFILE,
            local_trust=LocalTrust(),
        )
    assert raised.value.code is KeyInspectionErrorCode.EXPIRED_KEY


def test_wrong_full_fingerprint_cannot_be_enrolled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(monkeypatch, _colon_output())

    with pytest.raises(KeyInspectionError) as raised:
        enroll_key(
            inspection,
            expected_fingerprint="D" * 40,
            required_capabilities={KeyCapability.SIGN},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert raised.value.code is KeyInspectionErrorCode.FINGERPRINT_MISMATCH

    with pytest.raises(OpenPGPContractError) as short_pin:
        enroll_key(
            inspection,
            expected_fingerprint="12345678",
            required_capabilities={KeyCapability.SIGN},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert short_pin.value.code is ContractErrorCode.INVALID_FINGERPRINT


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("r", KeyInspectionErrorCode.REVOKED_KEY),
        ("e", KeyInspectionErrorCode.EXPIRED_KEY),
    ],
)
def test_revoked_or_expired_selected_subkey_is_reported_and_rejected(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: KeyInspectionErrorCode,
) -> None:
    inspection = _inspect_certificate(
        monkeypatch,
        _colon_output(subkey_status=status),
    )

    if status == "r":
        assert inspection.revoked_fingerprints == (ENCRYPTION_SUBKEY,)
    else:
        assert inspection.expired_fingerprints == (ENCRYPTION_SUBKEY,)
    with pytest.raises(KeyInspectionError) as raised:
        enroll_key(
            inspection,
            expected_fingerprint=PRIMARY,
            required_capabilities={KeyCapability.ENCRYPT},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert raised.value.code is expected_code


def test_revoked_historical_subkey_does_not_block_healthy_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(
        monkeypatch,
        _colon_output(add_revoked_subkey=True),
    )

    assert inspection.revoked_fingerprints == (REVOKED_SUBKEY,)
    assert inspection.encryption_subkey_fingerprints == (ENCRYPTION_SUBKEY,)
    enrollment = enroll_key(
        inspection,
        expected_fingerprint=PRIMARY,
        required_capabilities={KeyCapability.ENCRYPT},
        profile=KeyProfile(),
        local_trust=LocalTrust(),
    )
    assert enrollment.fingerprint == PRIMARY


def test_missing_selected_capability_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(
        monkeypatch,
        _colon_output(subkey_capabilities="", include_subkey=False),
    )

    with pytest.raises(OpenPGPContractError) as raised:
        enroll_key(
            inspection,
            expected_fingerprint=PRIMARY,
            required_capabilities={KeyCapability.ENCRYPT},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert raised.value.code is ContractErrorCode.MISSING_CAPABILITY


def test_profile_invalid_key_cannot_be_enrolled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = _inspect_certificate(
        monkeypatch,
        _colon_output(primary_bits=2048),
    )

    with pytest.raises(OpenPGPContractError) as raised:
        enroll_key(
            inspection,
            expected_fingerprint=PRIMARY,
            required_capabilities={KeyCapability.SIGN},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert raised.value.code is ContractErrorCode.INVALID_PROFILE


def test_unverified_inspection_object_cannot_authorize_enrollment() -> None:
    created = datetime.now(timezone.utc).date()
    record = KeyRecord(
        fingerprint=PRIMARY,
        algorithm="RSA",
        bits=4096,
        capabilities=frozenset({KeyCapability.SIGN}),
        created_on=created,
        expires_on=expected_expiry_date(created),
        is_primary=True,
    )

    with pytest.raises(KeyInspectionError) as raised:
        enroll_key(
            KeyInspection(record, ()),
            expected_fingerprint=PRIMARY,
            required_capabilities={KeyCapability.SIGN},
            profile=KeyProfile(),
            local_trust=LocalTrust(),
        )
    assert raised.value.code is KeyInspectionErrorCode.INVALID_ENROLLMENT


def test_unknown_reported_capability_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gpg(monkeypatch, _colon_output(primary_capabilities="SX"))

    with pytest.raises(KeyInspectionError) as raised:
        inspect_key(certificate=CERTIFICATE)
    assert raised.value.code is KeyInspectionErrorCode.INVALID_CAPABILITIES


def test_armored_private_key_material_is_rejected_before_gpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("private material must be rejected before GnuPG")

    monkeypatch.setattr(keys_api, "_run_gpg", must_not_run)
    private_key = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "synthetic private material\n"
        "-----END PGP PRIVATE KEY BLOCK-----\n"
    )
    with pytest.raises(KeyInspectionError) as raised:
        inspect_key(certificate=private_key)
    assert raised.value.code is KeyInspectionErrorCode.INVALID_SOURCE


def test_oversize_certificate_text_is_rejected_before_encoding() -> None:
    class EncodingTrap(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            pytest.fail("oversize text must be rejected before encoding")

    source = EncodingTrap("A" * (keys_api._MAX_CERTIFICATE_BYTES + 1))
    with pytest.raises(KeyInspectionError) as raised:
        inspect_key(certificate=source)
    assert raised.value.code is KeyInspectionErrorCode.INVALID_SOURCE


def test_explicit_keyring_reads_only_isolated_public_keybox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home = tmp_path / "synthetic-keyring"
    source_home.mkdir()
    keybox = source_home / "pubring.kbx"
    keybox.write_bytes(b"synthetic public keybox")
    trustdb = source_home / "trustdb.gpg"
    trustdb.write_bytes(b"consumer trust state")
    original_keybox = keybox.read_bytes()
    calls = _fake_gpg(monkeypatch, _colon_output())

    inspection = inspect_key(
        keyring_home=source_home,
        fingerprint=PRIMARY,
    )

    assert inspection.primary_fingerprint == PRIMARY
    assert keybox.read_bytes() == original_keybox
    assert trustdb.read_bytes() == b"consumer trust state"
    _, list_args, isolated_files = next(
        entry for entry in calls if "--list-keys" in entry[1]
    )
    assert "--homedir" not in list_args
    assert isolated_files == frozenset({"pubring.kbx"})

    with pytest.raises(KeyInspectionError) as missing:
        inspect_key(keyring_home=source_home, fingerprint="D" * 40)
    assert missing.value.code is KeyInspectionErrorCode.KEY_NOT_FOUND


@pytest.mark.parametrize("store_marker", ["public-keys.d", "pubring.gpg"])
def test_keyboxd_and_legacy_keyrings_are_not_implicitly_opened(
    tmp_path: Path,
    store_marker: str,
) -> None:
    source_home = tmp_path / "unsupported-keyring"
    source_home.mkdir()
    marker = source_home / store_marker
    if store_marker == "public-keys.d":
        marker.mkdir()
    else:
        marker.write_bytes(b"legacy keyring")

    with pytest.raises(KeyInspectionError) as raised:
        inspect_key(keyring_home=source_home, fingerprint=PRIMARY)
    assert raised.value.code is KeyInspectionErrorCode.UNSUPPORTED_KEYRING

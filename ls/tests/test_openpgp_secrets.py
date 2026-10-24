from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

import ls.core.openpgp.secrets as secret_module
import ls.core.openpgp.secret_files as secret_file_module
from ls.core.openpgp import (
    KeyProfile,
    LocalTrust,
    SecretProvider,
    SecretReference,
    SecretResolutionError,
    SecretResolutionErrorCode,
    SecretResolver,
    describe_generation_operation,
    describe_seal_operation,
)
from ls.core.openpgp.generation_models import KeyIdentity


def _install_fake_envman(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> Path:
    args_path = tmp_path / "envman-args.json"
    executable = tmp_path / "envman"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"with open({str(args_path)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump(sys.argv[1:], f)\n"
        + body,
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("ENVMAN_TEST_ARGS_FILE", str(args_path))
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(tmp_path), os.environ.get("PATH", "")))
    )
    return args_path


def _resolve_error(
    resolver: SecretResolver, reference: SecretReference
) -> SecretResolutionError:
    with pytest.raises(SecretResolutionError) as raised:
        resolver.resolve(reference)
    return raised.value


def test_secret_reference_requires_explicit_provider_and_valid_name() -> None:
    reference = SecretReference(provider="env", name="OPENPGP_PRIVATE_KEY")

    assert reference.provider is SecretProvider.ENV
    assert reference.name == "OPENPGP_PRIVATE_KEY"
    with pytest.raises(SecretResolutionError) as raised:
        SecretReference(provider="environment", name="value-must-not-echo")
    assert raised.value.code is SecretResolutionErrorCode.INVALID_REFERENCE
    assert str(raised.value) == "INVALID_REFERENCE"
    assert "value-must-not-echo" not in str(raised.value)

    with pytest.raises(SecretResolutionError) as invalid_name:
        SecretReference(provider="env", name="OPENPGP-KEY")
    assert invalid_name.value.code is SecretResolutionErrorCode.INVALID_REFERENCE
    assert str(invalid_name.value) == "INVALID_REFERENCE"


def test_env_provider_reads_current_value_and_missing_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_path = _install_fake_envman(
        tmp_path,
        monkeypatch,
        "print(json.dumps({'variable': {'name': 'OPENPGP_PRIVATE_KEY', 'value': 'envman-value'}}))\n",
    )
    reference = SecretReference(provider=SecretProvider.ENV, name="OPENPGP_PRIVATE_KEY")
    resolver = SecretResolver()

    monkeypatch.setenv(reference.name, "first-value")
    assert resolver.resolve(reference) == "first-value"
    monkeypatch.setenv(reference.name, "second-value")
    assert resolver.resolve(reference) == "second-value"
    monkeypatch.delenv(reference.name)
    error = _resolve_error(resolver, reference)

    assert error.code is SecretResolutionErrorCode.MISSING_SECRET
    assert str(error) == "MISSING_SECRET"
    assert not args_path.exists()


def test_envman_resolution_keeps_value_out_of_reference_and_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "synthetic-openpgp-secret-7fb912"
    value_file = tmp_path / "envman-value"
    value_file.write_text(value, encoding="utf-8")
    monkeypatch.setenv("ENVMAN_TEST_VALUE_FILE", str(value_file))
    args_path = _install_fake_envman(
        tmp_path,
        monkeypatch,
        f"with open({str(value_file)!r}, encoding='utf-8') as f:\n"
        "    value = f.read()\n"
        "print(json.dumps({'variable': {'name': 'OPENPGP_PRIVATE_KEY', "
        "'value': value, 'source': 'envman'}, 'metadata': {'version': 1}}))\n",
    )
    reference = SecretReference(
        provider=SecretProvider.ENVMAN, name="OPENPGP_PRIVATE_KEY"
    )
    resolver = SecretResolver()

    assert resolver.resolve(reference) == value
    next_value = "synthetic-openpgp-secret-rotated"
    value_file.write_text(next_value, encoding="utf-8")
    assert resolver.resolve(reference) == next_value
    assert json.loads(args_path.read_text(encoding="utf-8")) == [
        "get",
        "--json",
        "--reveal",
        "OPENPGP_PRIVATE_KEY",
    ]
    assert value not in repr(reference)
    assert value not in json.dumps(asdict(reference))

def test_explicit_envman_executable_works_with_sanitized_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_path = _install_fake_envman(
        tmp_path, monkeypatch,
        "print(json.dumps({'variable': {'name': 'OPENPGP_PRIVATE_KEY', 'value': 'selected-secret'}}))\n",
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    reference = SecretReference(SecretProvider.ENVMAN, "OPENPGP_PRIVATE_KEY")
    assert SecretResolver(envman_binary=tmp_path / "envman").resolve(reference) == "selected-secret"
    assert json.loads(args_path.read_text()) == ["get", "--json", "--reveal", reference.name]
    with pytest.raises(SecretResolutionError) as invalid:
        SecretResolver(envman_binary="other-relative-binary")
    assert invalid.value.code is SecretResolutionErrorCode.INVALID_REFERENCE


def test_envman_child_does_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALSETUP_RUN_CREDENTIAL', 'provider-secret')
    monkeypatch.setenv('LOCALSETUP_RUN_OPENPGP_PASSPHRASE', 'pgp-secret')
    monkeypatch.setenv('UNRELATED_API_KEY', 'other-secret')
    _install_fake_envman(tmp_path, monkeypatch,
        "assert not any(name in os.environ for name in "
        "('LOCALSETUP_RUN_CREDENTIAL', 'LOCALSETUP_RUN_OPENPGP_PASSPHRASE', 'UNRELATED_API_KEY'))\n"
        "print(json.dumps({'variable': {'name':'KEY', 'value':'selected-secret'}}))\n")
    assert SecretResolver().resolve(SecretReference('envman', 'KEY')) == 'selected-secret'


def test_envman_malformed_response_and_stderr_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "synthetic-malformed-secret-0a31"
    _install_fake_envman(
        tmp_path,
        monkeypatch,
        f"sys.stdout.write('malformed {value}')\n"
        f"sys.stderr.write('diagnostic {value}')\n",
    )
    reference = SecretReference(provider="envman", name="OPENPGP_PASSPHRASE")

    error = _resolve_error(SecretResolver(), reference)

    assert error.code is SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
    assert str(error) == "INVALID_SOURCE_RESPONSE"
    assert value not in str(error)


def test_envman_failure_does_not_fallback_to_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "synthetic-no-fallback-secret-2bb7"
    args_path = _install_fake_envman(
        tmp_path,
        monkeypatch,
        f"sys.stderr.write('failed {value}')\nsys.exit(7)\n",
    )
    reference = SecretReference(provider="envman", name="OPENPGP_PASSPHRASE")
    monkeypatch.setenv(reference.name, value)

    error = _resolve_error(SecretResolver(), reference)

    assert error.code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE
    assert str(error) == "SOURCE_UNAVAILABLE"
    assert value not in str(error)
    assert json.loads(args_path.read_text(encoding="utf-8")) == [
        "get",
        "--json",
        "--reveal",
        reference.name,
    ]


def test_envman_output_is_bounded_and_timeout_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = SecretReference(provider="envman", name="OPENPGP_PRIVATE_KEY")
    monkeypatch.setattr(secret_module, "_ENV_MAN_MAX_OUTPUT_BYTES", 128)
    _install_fake_envman(tmp_path, monkeypatch, "sys.stdout.write('x' * 4096)\n")

    oversized = _resolve_error(SecretResolver(), reference)

    assert oversized.code is SecretResolutionErrorCode.SOURCE_OUTPUT_LIMIT
    assert str(oversized) == "SOURCE_OUTPUT_LIMIT"

    monkeypatch.setattr(secret_module, "_ENV_MAN_TIMEOUT_SECONDS", 0.1)
    _install_fake_envman(tmp_path, monkeypatch, "import time\ntime.sleep(5)\n")

    timeout = _resolve_error(SecretResolver(), reference)

    assert timeout.code is SecretResolutionErrorCode.SOURCE_TIMEOUT
    assert str(timeout) == "SOURCE_TIMEOUT"


def test_generation_operation_accepts_the_existing_explicit_secret_provider(
    tmp_path: Path,
) -> None:
    operation = describe_generation_operation(
        identity=KeyIdentity("Synthetic User"),
        profile=KeyProfile(),
        keyring_home=tmp_path / "new-key-home",
    )
    reference = SecretReference(SecretProvider.ENV, "OPENPGP_GENERATION_PASSPHRASE")
    resolver = SecretResolver(env_values={reference.name: "synthetic-generation-secret"})

    secret = resolver.resolve_buffer(reference, operation=operation)

    assert operation.purpose == "generate-key"
    assert secret == b"synthetic-generation-secret"
    assert isinstance(secret, bytearray)
    secret[:] = b"\x00" * len(secret)
    secret.clear()


def _private_secret_root(tmp_path: Path) -> Path:
    root = tmp_path / "protected-secrets"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _write_private_secret(root: Path, name: str, value: bytes) -> Path:
    path = root / name
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def _protected_file_reference(name: str = "OPENPGP_PASSPHRASE") -> SecretReference:
    return SecretReference(SecretProvider.PROTECTED_FILE, name)


def test_protected_file_provider_reads_only_the_explicit_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "OPENPGP_PASSPHRASE"
    value = "synthetic-protected-file-secret-100%"
    root = _private_secret_root(tmp_path)
    _write_private_secret(root, name, value.encode("utf-8"))
    monkeypatch.setenv(name, "environment-must-not-be-used")
    reference = _protected_file_reference(name)
    resolver = SecretResolver(protected_file_root=root)

    assert resolver.resolve(reference) == value
    assert value not in repr(reference)
    assert str(_resolve_error(SecretResolver(), reference)) == "SOURCE_UNAVAILABLE"
    empty_parent = tmp_path / "empty"
    empty_parent.mkdir(mode=0o700)
    empty_root = _private_secret_root(empty_parent)
    assert str(
        _resolve_error(
            SecretResolver(protected_file_root=empty_root), reference
        )
    ) == "MISSING_SECRET"


def test_protected_file_provider_requires_a_valid_root_and_symbolic_name() -> None:
    with pytest.raises(SecretResolutionError) as relative_root:
        SecretResolver(protected_file_root="relative/secrets")
    assert relative_root.value.code is SecretResolutionErrorCode.INVALID_REFERENCE
    with pytest.raises(SecretResolutionError) as traversal:
        SecretReference(SecretProvider.PROTECTED_FILE, "../outside")
    assert traversal.value.code is SecretResolutionErrorCode.INVALID_REFERENCE


def test_protected_file_provider_rejects_symlinked_ancestors_and_leaves(
    tmp_path: Path,
) -> None:
    target = _private_secret_root(tmp_path)
    _write_private_secret(target, "OPENPGP_PASSPHRASE", b"fixture-secret")
    resolver = SecretResolver()
    reference = _protected_file_reference()

    root_link = tmp_path / "root-link"
    root_link.symlink_to(target, target_is_directory=True)
    assert _resolve_error(
        SecretResolver(protected_file_root=root_link), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE

    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(tmp_path, target_is_directory=True)
    assert _resolve_error(
        SecretResolver(protected_file_root=ancestor_link / target.name), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE

    secret_path = target / reference.name
    secret_path.unlink()
    secret_path.symlink_to(target / "real-secret")
    (target / "real-secret").write_text("fixture-secret", encoding="utf-8")
    (target / "real-secret").chmod(0o600)
    assert _resolve_error(
        SecretResolver(protected_file_root=target), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE


def test_protected_file_provider_rejects_unsafe_root_and_file_modes(
    tmp_path: Path,
) -> None:
    root = _private_secret_root(tmp_path)
    path = _write_private_secret(root, "OPENPGP_PASSPHRASE", b"fixture-secret")
    reference = _protected_file_reference()

    root.chmod(0o750)
    assert _resolve_error(
        SecretResolver(protected_file_root=root), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE
    assert stat.S_IMODE(root.stat().st_mode) == 0o750

    root.chmod(0o700)
    path.chmod(0o640)
    assert _resolve_error(
        SecretResolver(protected_file_root=root), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE


def test_protected_file_provider_rejects_hardlinks_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    root = _private_secret_root(tmp_path)
    path = _write_private_secret(root, "OPENPGP_PASSPHRASE", b"fixture-secret")
    reference = _protected_file_reference()
    os.link(path, root / "second-link")
    assert _resolve_error(
        SecretResolver(protected_file_root=root), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE

    path.unlink()
    (root / "second-link").unlink()
    os.mkfifo(path)
    assert _resolve_error(
        SecretResolver(protected_file_root=root), reference
    ).code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE


def test_protected_file_provider_checks_owner_and_replacement_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_secret_root(tmp_path)
    name = "OPENPGP_PASSPHRASE"
    path = _write_private_secret(root, name, b"original-fixture-secret")
    reference = _protected_file_reference(name)
    real_fstat = os.fstat
    real_stat = os.stat

    def foreign_owner(info):
        if not stat.S_ISREG(info.st_mode):
            return info
        fields = {
            field: getattr(info, field)
            for field in (
                "st_mode", "st_uid", "st_nlink", "st_size", "st_dev",
                "st_ino", "st_mtime_ns", "st_ctime_ns",
            )
        }
        fields["st_uid"] = os.geteuid() + 1
        return SimpleNamespace(**fields)

    def fake_fstat(descriptor: int):
        return foreign_owner(real_fstat(descriptor))

    def fake_stat(selected, *args, **kwargs):
        info = real_stat(selected, *args, **kwargs)
        return foreign_owner(info) if selected == name else info

    with monkeypatch.context() as patch:
        patch.setattr(secret_file_module.os, "fstat", fake_fstat)
        patch.setattr(secret_file_module.os, "stat", fake_stat)
        owner_error = _resolve_error(SecretResolver(protected_file_root=root), reference)
    assert owner_error.code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE

    real_read = os.read
    replaced = False

    def replace_during_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        payload = real_read(descriptor, size)
        if not replaced:
            replaced = True
            path.unlink()
            _write_private_secret(root, name, b"replacement-fixture-secret")
        return payload

    with monkeypatch.context() as patch:
        patch.setattr(secret_file_module.os, "read", replace_during_read)
        changed_error = _resolve_error(
            SecretResolver(protected_file_root=root), reference
        )
    assert changed_error.code is SecretResolutionErrorCode.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"", SecretResolutionErrorCode.SOURCE_UNAVAILABLE),
        (b"fixture\xff", SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE),
        (b"fixture\n", SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE),
        (b"fixture\x00", SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE),
    ],
)
def test_protected_file_provider_validates_content(
    tmp_path: Path,
    value: bytes,
    expected: SecretResolutionErrorCode,
) -> None:
    root = _private_secret_root(tmp_path)
    _write_private_secret(root, "OPENPGP_PASSPHRASE", value)

    error = _resolve_error(
        SecretResolver(protected_file_root=root), _protected_file_reference()
    )

    assert error.code is expected
    assert str(error) == expected.value


def test_protected_file_provider_bounds_reads_and_redacts_errors(
    tmp_path: Path,
) -> None:
    root = _private_secret_root(tmp_path)
    value = "x" * 4097
    secret_path = _write_private_secret(
        root, "OPENPGP_PASSPHRASE", value.encode()
    )

    error = _resolve_error(
        SecretResolver(protected_file_root=root), _protected_file_reference()
    )

    assert error.code is SecretResolutionErrorCode.SOURCE_OUTPUT_LIMIT
    assert value not in str(error)
    assert str(secret_path) not in str(error)
    assert str(error) == "SOURCE_OUTPUT_LIMIT"

def test_create_protected_secret_returns_only_reference_and_created_state(
    tmp_path: Path,
) -> None:
    root = _private_secret_root(tmp_path)
    name = "OPENPGP_GENERATED_PASSPHRASE"
    path = root / name

    reference, created = secret_module.create_protected_secret(root, name)

    assert reference == _protected_file_reference(name)
    assert created is True
    first_value = path.read_bytes()
    assert len(first_value) == 43
    assert first_value.decode("ascii") not in repr((reference, created))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1

    reused_reference, created_again = secret_module.create_protected_secret(
        root, name
    )

    assert reused_reference == reference
    assert created_again is False
    assert path.read_bytes() == first_value
    assert SecretResolver(protected_file_root=root).resolve(reference) == (
        first_value.decode("ascii")
    )


def test_create_protected_secret_preserves_corrupt_or_partial_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _private_secret_root(tmp_path)
    name = "OPENPGP_GENERATED_PASSPHRASE"
    path = _write_private_secret(root, name, b"corrupt-but-must-be-preserved")

    with pytest.raises(SecretResolutionError) as corrupt:
        secret_module.create_protected_secret(root, name)
    assert corrupt.value.code is SecretResolutionErrorCode.PARTIAL_SECRET
    assert str(corrupt.value) == "PARTIAL_SECRET"
    assert str(path) not in str(corrupt.value)
    assert path.read_bytes() == b"corrupt-but-must-be-preserved"

    path.unlink()

    def fail_after_partial_write(descriptor: int, payload: bytearray) -> None:
        os.write(descriptor, payload[:7])
        raise OSError("synthetic private write failure")

    monkeypatch.setattr(secret_file_module, "_write_all", fail_after_partial_write)
    with pytest.raises(SecretResolutionError) as partial:
        secret_module.create_protected_secret(root, name)
    assert partial.value.code is SecretResolutionErrorCode.PARTIAL_SECRET
    assert str(partial.value) == "PARTIAL_SECRET"
    partial_contents = path.read_bytes()
    assert len(partial_contents) == 7

    with pytest.raises(SecretResolutionError) as retry:
        secret_module.create_protected_secret(root, name)
    assert retry.value.code is SecretResolutionErrorCode.PARTIAL_SECRET
    assert path.read_bytes() == partial_contents


def test_concurrent_protected_secret_creation_has_one_winner_and_safe_reuse(
    tmp_path: Path,
) -> None:
    root = _private_secret_root(tmp_path)
    name = "OPENPGP_GENERATED_PASSPHRASE"

    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(secret_module.create_protected_secret, root, name)
            for _ in range(2)
        ]
        for future in futures:
            try:
                results.append(future.result())
            except SecretResolutionError as error:
                assert error.code is SecretResolutionErrorCode.PARTIAL_SECRET

    successful = results
    assert successful
    references = [reference for reference, _created in successful]
    assert all(reference == references[0] for reference in references)
    assert sum(created for _reference, created in successful) == 1

    path = root / name
    value = path.read_bytes()
    assert len(value) == 43
    retry_reference, retry_created = secret_module.create_protected_secret(root, name)
    assert retry_reference == references[0]
    assert retry_created is False
    assert path.read_bytes() == value
    assert value.decode("ascii") not in repr(successful)


@pytest.mark.parametrize("operation", ["read", "create", "reuse"])
def test_protected_secret_root_replacement_fails_without_removing_either_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    root = _private_secret_root(tmp_path)
    name = "ROOT_REPLACEMENT_FIXTURE"
    retained = root.with_name("retained-root")
    if operation != "create":
        _write_private_secret(root, name, b"A" * 43)
    replaced = False

    def replace_root():
        nonlocal replaced
        if not replaced:
            replaced = True
            root.rename(retained)
            root.mkdir(mode=0o700)
            _write_private_secret(root, name, b"B" * 43)

    if operation == "create":
        original = secret_file_module._write_all

        def write(descriptor, payload):
            original(descriptor, payload)
            replace_root()

        monkeypatch.setattr(secret_file_module, "_write_all", write)
    else:
        original = secret_file_module.os.read

        def read(descriptor, size):
            data = original(descriptor, size)
            replace_root()
            return data

        monkeypatch.setattr(secret_file_module.os, "read", read)
    with pytest.raises(SecretResolutionError) as denied:
        if operation == "read":
            SecretResolver(protected_file_root=root).resolve(_protected_file_reference(name))
        else:
            secret_module.create_protected_secret(root, name)
    expected = "SOURCE_UNAVAILABLE" if operation == "read" else "PARTIAL_SECRET"
    assert denied.value.code.value == expected
    assert replaced
    assert (root / name).read_bytes() == b"B" * 43
    assert len((retained / name).read_bytes()) == 43


def test_resolve_buffer_preserves_explicit_legacy_provider_behavior(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    reference = SecretReference(SecretProvider.ENV, "OPENPGP_PASSPHRASE")
    operation = describe_seal_operation(
        payload=b"fixture payload",
        signer_fingerprint="A" * 40,
        recipient_fingerprints=("B" * 40,),
        local_trust=LocalTrust({"A" * 40}),
        gnupg_home=home,
    )
    buffer = SecretResolver(env_values={reference.name: "legacy-value"}).resolve_buffer(
        reference, operation=operation
    )

    assert isinstance(buffer, bytearray)
    assert buffer == b"legacy-value"
    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


@pytest.mark.parametrize("value", ["bad\x00secret", "bad\rsecret", "bad\nsecret", "x" * 4097])
def test_resolve_buffer_rejects_invalid_legacy_secret_text(value: str, tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    reference = SecretReference(SecretProvider.ENV, "OPENPGP_PASSPHRASE")
    operation = describe_seal_operation(
        payload=b"fixture payload",
        signer_fingerprint="A" * 40,
        recipient_fingerprints=("B" * 40,),
        local_trust=LocalTrust({"A" * 40}),
        gnupg_home=home,
    )

    with pytest.raises(SecretResolutionError) as raised:
        SecretResolver(env_values={reference.name: value}).resolve_buffer(
            reference, operation=operation
        )

    assert raised.value.code is SecretResolutionErrorCode.INVALID_SOURCE_RESPONSE
    assert value not in str(raised.value)

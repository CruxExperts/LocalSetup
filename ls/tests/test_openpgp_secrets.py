from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

import pytest

import ls.core.openpgp.secrets as secret_module
from ls.core.openpgp import (
    SecretProvider,
    SecretReference,
    SecretResolutionError,
    SecretResolutionErrorCode,
    SecretResolver,
)


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
        "with open(os.environ['ENVMAN_TEST_ARGS_FILE'], 'w', encoding='utf-8') as f:\n"
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
        "with open(os.environ['ENVMAN_TEST_VALUE_FILE'], encoding='utf-8') as f:\n"
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

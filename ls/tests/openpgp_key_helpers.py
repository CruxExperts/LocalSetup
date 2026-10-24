"""Disposable real GnuPG fixtures shared by file and container tests."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ls.core.openpgp import recovery_process

PASSPHRASE = "artifact-test-only-passphrase"


def _gpg(home: Path, *arguments: str, input_data: bytes | None = None) -> bytes:
    command = [
        "gpg", "--no-options", "--batch", "--no-tty", "--homedir", str(home),
        *arguments,
    ]
    return subprocess.run(
        command,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=60,
    ).stdout


def _create_key(home: Path, label: str) -> tuple[str, bytes]:
    uid = f"Artifact {label} <{label.lower()}@example.invalid>"
    _gpg(
        home,
        "--pinentry-mode", "loopback", "--passphrase-fd", "0",
        "--quick-generate-key", uid, "rsa2048", "sign", "1d",
        input_data=(PASSPHRASE + "\n").encode(),
    )
    listing = _gpg(home, "--with-colons", "--list-keys", uid).decode("utf-8")
    fingerprint = next(
        line.split(":")[9]
        for line in listing.splitlines()
        if line.startswith("fpr:")
    )
    _gpg(
        home,
        "--pinentry-mode", "loopback", "--passphrase-fd", "0",
        "--quick-add-key", fingerprint, "rsa2048", "encr", "1d",
        input_data=(PASSPHRASE + "\n").encode(),
    )
    return fingerprint, _gpg(home, "--export", fingerprint)


def _register_gpg_home_cleanup(request: pytest.FixtureRequest, home: Path) -> None:
    """Stop and verify the agent for this newly created disposable home."""
    def cleanup() -> None:
        if not recovery_process._home_agent_is_running(home):
            return
        if not recovery_process._stop_home_agent(home):
            raise AssertionError(f"could not stop disposable GPG agent for {home}")
        if recovery_process._home_agent_is_running(home):
            raise AssertionError(f"disposable GPG agent is still running for {home}")

    request.addfinalizer(cleanup)

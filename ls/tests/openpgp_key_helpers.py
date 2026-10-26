"""Disposable real GnuPG fixtures shared by file and container tests."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ls.core.openpgp import recovery_process

PASSPHRASE = "artifact-test-only-passphrase"
_STATUS_PREFIX = b"[GNUPG:] "
_DIAGNOSTIC_STATUSES = frozenset({
    b"ERROR", b"FAILURE", b"KEY_NOT_CREATED", b"BAD_PASSPHRASE",
    b"MISSING_PASSPHRASE", b"NO_SECKEY", b"NO_PUBKEY",
})


def _failure_phase(arguments: tuple[str, ...]) -> str:
    for option, phase in (
        ("--quick-generate-key", "generate_primary"),
        ("--quick-add-key", "add_subkey"),
        ("--import", "import"),
        ("--export", "export"),
        ("--list-secret-keys", "list_secret"),
        ("--list-keys", "list_public"),
    ):
        if option in arguments:
            return phase
    return "other"


def _failure_status(stderr: bytes | None) -> str:
    statuses: list[str] = []
    for line in (stderr or b"")[-8192:].splitlines():
        if not line.startswith(_STATUS_PREFIX):
            continue
        fields = line[len(_STATUS_PREFIX):].split()
        if not fields or fields[0] not in _DIAGNOSTIC_STATUSES:
            continue
        status = fields[0].decode("ascii")
        if (fields[0] in {b"ERROR", b"FAILURE"} and len(fields) >= 2
                and fields[-1].isascii() and fields[-1].isdigit()
                and len(fields[-1]) <= 10):
            status += ":" + fields[-1].decode("ascii")
        if status not in statuses:
            statuses.append(status)
        if len(statuses) == 4:
            break
    return ",".join(statuses) if statuses else "unknown"


def _gpg(home: Path, *arguments: str, input_data: bytes | None = None) -> bytes:
    command = [
        "gpg", "--no-options", "--batch", "--no-tty", "--status-fd=2",
        "--homedir", str(home),
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        ).stdout
    except subprocess.CalledProcessError as exc:
        exc.add_note(
            f"GPG fixture phase={_failure_phase(arguments)} "
            f"returncode={exc.returncode} status={_failure_status(exc.stderr)}"
        )
        raise


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

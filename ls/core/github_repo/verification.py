"""Bounded local verification for signed Git objects and release artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any

from . import local_evidence


_MAX_COMMAND_OUTPUT = 1024 * 1024
_MAX_COMMAND_SECONDS = 8.0
_MAX_PUBLIC_KEY_BYTES = 128 * 1024
_MAX_PUBLIC_KEYS = 32
_MAX_TOTAL_PUBLIC_KEY_BYTES = 1024 * 1024
_MAX_ARTIFACTS = 100
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_ARTIFACT_BYTES = 500 * 1024 * 1024
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FINGERPRINT = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BAD_SIGNATURE_STATUSES = {
    "BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "KEYEXPIRED", "REVKEYSIG", "KEYREVOKED", "SIGEXPIRED",
}


class _VerificationFailure(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _record(status: str, reason: str | None, **values: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **values}


def _sanitized_env(gnupg_home: Path | None = None) -> dict[str, str]:
    """Build an environment that cannot inherit Git or GPG user configuration."""
    env: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    })
    if gnupg_home is not None:
        env["GNUPGHOME"] = os.fspath(gnupg_home)
        env["HOME"] = os.fspath(gnupg_home)
        env["GPG_TTY"] = ""
    return env


def _kill_process_tree(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    if process.poll() is not None and not (force and os.name == "posix"):
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = subprocess.DEVNULL,
) -> tuple[bytes, bytes, int]:
    """Run one fixed local command with a time and combined-output limit."""
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env if env is not None else _sanitized_env(),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        raise _VerificationFailure("unavailable", "required_tool_unavailable") from exc
    except OSError as exc:
        raise _VerificationFailure("unavailable", "local_command_unavailable") from exc

    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    selector = selectors.DefaultSelector()
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    captured = 0
    deadline = time.monotonic() + _MAX_COMMAND_SECONDS
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _VerificationFailure("unavailable", "local_command_timeout")
            ready = selector.select(remaining)
            if not ready:
                raise _VerificationFailure("unavailable", "local_command_timeout")
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), min(65536, _MAX_COMMAND_OUTPUT + 1 - captured))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > _MAX_COMMAND_OUTPUT:
                    raise _VerificationFailure("unavailable", "local_command_output_limit_exceeded")
                output[key.fileobj].extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise _VerificationFailure("unavailable", "local_command_timeout") from exc
        return bytes(output[process.stdout]), bytes(output[process.stderr]), returncode
    finally:
        unfinished_streams = bool(selector.get_map())
        selector.close()
        _kill_process_tree(process, force=unfinished_streams)
        for stream in streams:
            stream.close()


def _git(git: str, root: Path, args: list[str], gnupg_home: Path | None = None) -> bytes:
    stdout = _git_bytes(git, root, args, gnupg_home)
    if not stdout.endswith(b"\n") or stdout.count(b"\n") != 1:
        raise _VerificationFailure("invalid", "local_git_evidence_invalid")
    return stdout[:-1]


def _git_bytes(git: str, root: Path, args: list[str], gnupg_home: Path | None = None) -> bytes:
    command = [git, "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args]
    stdout, _stderr, returncode = _run_bounded(command, cwd=root, env=_sanitized_env(gnupg_home))
    if returncode != 0:
        raise _VerificationFailure("invalid", "local_git_evidence_invalid")
    return stdout


def _check_tag_ref(git: str, root: Path, tag_name: str, gnupg_home: Path | None = None) -> None:
    ref_name = f"refs/tags/{tag_name}"
    _stdout, _stderr, returncode = _run_bounded(
        [git, "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "check-ref-format", ref_name],
        cwd=root,
        env=_sanitized_env(gnupg_home),
    )
    if returncode != 0:
        raise _VerificationFailure("invalid", "tag_name_invalid")


def _resolve_checkout(checkout_path: str | os.PathLike[str]) -> Path:
    if not isinstance(checkout_path, (str, os.PathLike)):
        raise _VerificationFailure("invalid", "invalid_checkout_path")
    try:
        root = Path(checkout_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise _VerificationFailure("unavailable", "checkout_missing") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _VerificationFailure("invalid", "invalid_checkout_path") from exc
    if not root.is_dir():
        raise _VerificationFailure("invalid", "checkout_not_directory")
    return root


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _discover_executable(name: str, checkout_root: Path) -> str | None:
    """Resolve tools only from absolute PATH entries outside the target checkout."""
    path_value = os.environ.get("PATH", "")
    candidates = [name]
    if os.name == "nt" and not Path(name).suffix:
        candidates = [name + suffix for suffix in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep) if suffix]
    for entry in path_value.split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        for candidate_name in candidates:
            candidate = resolved_directory / candidate_name
            try:
                executable = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not executable.is_file() or not os.access(executable, os.X_OK):
                continue
            if _path_is_within(executable, checkout_root):
                continue
            return os.fspath(executable)
    return None


def _validate_checkout(root: Path, git: str) -> Path:
    try:
        output = _git(git, root, ["rev-parse", "--show-toplevel"])
        resolved_git_root = Path(os.fsdecode(output)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _VerificationFailure("unavailable", "not_git_repository") from exc
    if not resolved_git_root.is_dir() or resolved_git_root != root:
        raise _VerificationFailure("invalid", "checkout_root_mismatch")
    return root


def _validate_tag_name(tag_name: Any) -> str:
    if (
        not isinstance(tag_name, str)
        or not tag_name
        or len(tag_name) > 256
        or "\x00" in tag_name
        or tag_name.startswith("-")
    ):
        raise _VerificationFailure("invalid", "tag_name_invalid")
    return tag_name


def load_trusted_public_keys(paths: Sequence[str | os.PathLike[str]]) -> list[bytes]:
    """Read caller-selected public key files without exposing paths in errors or output."""
    try:
        if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence) or len(paths) > _MAX_PUBLIC_KEYS:
            raise _VerificationFailure("invalid", "trusted_key_input_invalid")
        keys: list[bytes] = []
        total = 0
        for path in paths:
            if not isinstance(path, (str, os.PathLike)):
                raise _VerificationFailure("invalid", "trusted_key_input_invalid")
            descriptor = os.open(
                os.fspath(Path(path).expanduser()),
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_PUBLIC_KEY_BYTES:
                    raise _VerificationFailure("invalid", "trusted_key_input_invalid")
                chunks = bytearray()
                while True:
                    chunk = os.read(descriptor, min(32 * 1024, _MAX_PUBLIC_KEY_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_PUBLIC_KEY_BYTES:
                        raise _VerificationFailure("invalid", "trusted_key_input_limit_exceeded")
                key = bytes(chunks)
            finally:
                os.close(descriptor)
            if b"-----BEGIN PGP PUBLIC KEY BLOCK-----" not in key or b"-----BEGIN PGP SECRET KEY BLOCK-----" in key or b"-----BEGIN PGP PRIVATE KEY BLOCK-----" in key:
                raise _VerificationFailure("invalid", "trusted_key_input_invalid")
            total += len(key)
            if total > _MAX_TOTAL_PUBLIC_KEY_BYTES:
                raise _VerificationFailure("invalid", "trusted_key_input_limit_exceeded")
            keys.append(key)
        return keys
    except _VerificationFailure as exc:
        raise ValueError("trusted public-key input is invalid") from exc
    except OSError as exc:
        raise ValueError("trusted public-key input is unavailable") from exc


def _normalize_trust_input(
    public_keys: Sequence[str | bytes] | None,
    primary_fingerprints: Sequence[str] | None,
) -> tuple[list[bytes], set[str]]:
    if public_keys is None or primary_fingerprints is None:
        raise _VerificationFailure("incomplete", "trusted_key_input_missing")
    if isinstance(public_keys, (str, bytes)) or isinstance(primary_fingerprints, str):
        raise _VerificationFailure("invalid", "trusted_key_input_invalid")
    if not isinstance(public_keys, Sequence) or not isinstance(primary_fingerprints, Sequence):
        raise _VerificationFailure("invalid", "trusted_key_input_invalid")
    if len(public_keys) == 0 or len(primary_fingerprints) == 0:
        raise _VerificationFailure("incomplete", "trusted_key_input_missing")
    if len(public_keys) > _MAX_PUBLIC_KEYS or len(primary_fingerprints) > _MAX_PUBLIC_KEYS:
        raise _VerificationFailure("invalid", "trusted_key_input_limit_exceeded")

    keys: list[bytes] = []
    total_bytes = 0
    for value in public_keys:
        if isinstance(value, str):
            try:
                key = value.encode("ascii", errors="strict")
            except UnicodeEncodeError as exc:
                raise _VerificationFailure("invalid", "trusted_key_input_invalid") from exc
        elif isinstance(value, bytes):
            key = value
        else:
            raise _VerificationFailure("invalid", "trusted_key_input_invalid")
        if (
            not key
            or len(key) > _MAX_PUBLIC_KEY_BYTES
            or b"-----BEGIN PGP PUBLIC KEY BLOCK-----" not in key
            or b"-----BEGIN PGP PRIVATE KEY BLOCK-----" in key
            or b"-----BEGIN PGP SECRET KEY BLOCK-----" in key
            or b"\x00" in key
        ):
            raise _VerificationFailure("invalid", "trusted_key_input_invalid")
        total_bytes += len(key)
        if total_bytes > _MAX_TOTAL_PUBLIC_KEY_BYTES:
            raise _VerificationFailure("invalid", "trusted_key_input_limit_exceeded")
        keys.append(key)

    fingerprints: set[str] = set()
    for value in primary_fingerprints:
        if not isinstance(value, str):
            raise _VerificationFailure("invalid", "trusted_fingerprint_invalid")
        normalized = value.upper()
        if not _FINGERPRINT.fullmatch(normalized) or normalized in fingerprints:
            raise _VerificationFailure("invalid", "trusted_fingerprint_invalid")
        fingerprints.add(normalized)
    return keys, fingerprints


def _parse_primary_fingerprints(output: bytes) -> set[str]:
    try:
        lines = output.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise _VerificationFailure("invalid", "trusted_keyring_invalid") from exc
    primary_fingerprints: set[str] = set()
    awaiting_primary = False
    for line in lines:
        fields = line.split(":")
        if fields[0] in {"sec", "ssb"}:
            raise _VerificationFailure("invalid", "trusted_secret_key_material_invalid")
        if fields[0] in {"pub", "sub", "sec", "ssb"}:
            awaiting_primary = fields[0] == "pub"
        elif fields[0] == "fpr" and awaiting_primary:
            if len(fields) < 10 or not _FINGERPRINT.fullmatch(fields[9].upper()):
                raise _VerificationFailure("invalid", "trusted_keyring_invalid")
            primary_fingerprints.add(fields[9].upper())
            awaiting_primary = False
    if not primary_fingerprints:
        raise _VerificationFailure("invalid", "trusted_keyring_invalid")
    return primary_fingerprints


def _import_trust_keys(gpg: str, home: Path, keys: list[bytes], expected: set[str]) -> None:
    key_file = home / "caller-public-keys.asc"
    try:
        key_file.write_bytes(b"\n".join(keys) + b"\n")
        key_file.chmod(0o600)
        _stdout, _stderr, code = _run_bounded(
            [gpg, "--no-options", "--batch", "--no-tty", "--homedir", os.fspath(home), "--import", os.fspath(key_file)],
            env=_sanitized_env(home),
        )
        if code != 0:
            raise _VerificationFailure("invalid", "trusted_key_import_failed")
        output, _stderr, code = _run_bounded(
            [gpg, "--no-options", "--batch", "--no-tty", "--homedir", os.fspath(home), "--with-colons", "--fingerprint", "--list-keys"],
            env=_sanitized_env(home),
        )
        if code != 0 or _parse_primary_fingerprints(output) != expected:
            raise _VerificationFailure("invalid", "trusted_key_fingerprint_mismatch")
        secret_output, _stderr, code = _run_bounded(
            [gpg, "--no-options", "--batch", "--no-tty", "--homedir", os.fspath(home), "--with-colons", "--list-secret-keys"],
            env=_sanitized_env(home),
        )
        if code != 0:
            raise _VerificationFailure("invalid", "trusted_keyring_invalid")
        try:
            secret_lines = secret_output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise _VerificationFailure("invalid", "trusted_keyring_invalid") from exc
        if any(line.startswith(("sec:", "ssb:")) for line in secret_lines):
            raise _VerificationFailure("invalid", "trusted_secret_key_material_invalid")
    except OSError as exc:
        raise _VerificationFailure("unavailable", "trusted_key_storage_unavailable") from exc


def _valid_signature_fingerprint(stdout: bytes, stderr: bytes, expected: set[str]) -> str:
    try:
        text = (stdout + b"\n" + stderr).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise _VerificationFailure("invalid", "signature_status_invalid") from exc
    statuses: list[list[str]] = []
    status_names: set[str] = set()
    for line in text.splitlines():
        marker = "[GNUPG:] "
        if line.startswith(marker):
            fields = line[len(marker):].split()
            if fields:
                status_names.add(fields[0])
                if fields[0] == "VALIDSIG":
                    statuses.append(fields)
    if status_names & _BAD_SIGNATURE_STATUSES:
        raise _VerificationFailure("invalid", "signature_invalid")
    if len(statuses) != 1 or len(statuses[0]) < 10:
        raise _VerificationFailure("invalid", "signature_missing_or_ambiguous")
    fields = statuses[0]
    signing_fingerprint = fields[1].upper()
    primary_fingerprint = fields[10].upper() if len(fields) > 10 else signing_fingerprint
    if not _FINGERPRINT.fullmatch(signing_fingerprint) or not _FINGERPRINT.fullmatch(primary_fingerprint):
        raise _VerificationFailure("invalid", "signature_status_invalid")
    if primary_fingerprint not in expected:
        raise _VerificationFailure("invalid", "signature_signer_not_trusted")
    return primary_fingerprint


def verify_release_signatures(
    checkout_path: str | os.PathLike[str],
    commit_object_id: str,
    tag_name: str,
    trusted_public_keys: Sequence[str | bytes] | None,
    trusted_primary_fingerprints: Sequence[str] | None,
) -> dict[str, Any]:
    """Verify one exact commit and annotated tag using caller-supplied OpenPGP keys.

    The returned evidence is derived from local Git objects and an ephemeral keyring.
    No remote ref lookup, key retrieval, signing, or repository-configured GPG program
    is used.
    """
    commit_oid: str | None = None
    normalized_tag: str | None = None
    try:
        if not isinstance(commit_object_id, str) or not _OBJECT_ID.fullmatch(commit_object_id):
            raise _VerificationFailure("invalid", "commit_object_id_invalid")
        commit_oid = commit_object_id
        normalized_tag = _validate_tag_name(tag_name)
        keys, expected_fingerprints = _normalize_trust_input(
            trusted_public_keys, trusted_primary_fingerprints,
        )
        root = _resolve_checkout(checkout_path)
        git = _discover_executable("git", root)
        gpg = _discover_executable("gpg", root)
        if git is None or gpg is None:
            raise _VerificationFailure("unavailable", "required_tool_unavailable")
        _validate_checkout(root, git)

        with tempfile.TemporaryDirectory(prefix="ls-github-verify-") as temporary:
            home = Path(temporary)
            home.chmod(0o700)
            (home / "gpg.conf").write_text(
                "no-auto-key-retrieve\nno-auto-check-trustdb\nbatch\nno-tty\n",
                encoding="ascii",
            )
            _import_trust_keys(gpg, home, keys, expected_fingerprints)

            object_type = _git(git, root, ["cat-file", "-t", commit_oid])
            if object_type != b"commit":
                raise _VerificationFailure("invalid", "selected_object_is_not_commit")

            commit_stdout, commit_stderr, commit_code = _run_bounded(
                [
                    git, "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                    "-c", "gpg.format=openpgp", "-c", f"gpg.program={gpg}",
                    "verify-commit", "--raw", commit_oid,
                ],
                cwd=root,
                env=_sanitized_env(home),
            )
            if commit_code != 0:
                raise _VerificationFailure("invalid", "commit_signature_invalid")
            commit_signer = _valid_signature_fingerprint(commit_stdout, commit_stderr, expected_fingerprints)

            ref_name = f"refs/tags/{normalized_tag}"
            _check_tag_ref(git, root, normalized_tag)
            tag_oid_bytes = _git(git, root, ["rev-parse", "--verify", "--end-of-options", f"{ref_name}^{{tag}}"])
            try:
                tag_oid = tag_oid_bytes.decode("ascii", errors="strict")
            except UnicodeDecodeError as exc:
                raise _VerificationFailure("invalid", "annotated_tag_invalid") from exc
            if not _OBJECT_ID.fullmatch(tag_oid):
                raise _VerificationFailure("invalid", "annotated_tag_invalid")
            tag_object = _git_bytes(git, root, ["cat-file", "-p", tag_oid], home)
            header, separator, _message = tag_object.partition(b"\n\n")
            if not separator:
                raise _VerificationFailure("invalid", "annotated_tag_invalid")
            tag_headers = [line[4:] for line in header.split(b"\n") if line.startswith(b"tag ")]
            try:
                selected_tag_bytes = normalized_tag.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise _VerificationFailure("invalid", "tag_name_invalid") from exc
            if len(tag_headers) != 1 or tag_headers[0] != selected_tag_bytes:
                raise _VerificationFailure("invalid", "annotated_tag_name_mismatch")
            peeled = _git(git, root, ["rev-parse", "--verify", "--end-of-options", f"{tag_oid}^{{}}"])
            if peeled.decode("ascii", errors="strict") != commit_oid:
                raise _VerificationFailure("invalid", "annotated_tag_target_mismatch")
            if _git(git, root, ["cat-file", "-t", tag_oid]) != b"tag":
                raise _VerificationFailure("invalid", "annotated_tag_required")

            tag_stdout, tag_stderr, tag_code = _run_bounded(
                [
                    git, "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                    "-c", "gpg.format=openpgp", "-c", f"gpg.program={gpg}",
                    "verify-tag", "--raw", tag_oid,
                ],
                cwd=root,
                env=_sanitized_env(home),
            )
            if tag_code != 0:
                raise _VerificationFailure("invalid", "tag_signature_invalid")
            tag_signer = _valid_signature_fingerprint(tag_stdout, tag_stderr, expected_fingerprints)
        return _record(
            "verified", None,
            evidence_scope="local_git_openpgp_signatures",
            commit_object_id=commit_oid,
            tag_name=normalized_tag,
            tag_object_id=tag_oid,
            commit_primary_fingerprint=commit_signer,
            tag_primary_fingerprint=tag_signer,
        )
    except _VerificationFailure as exc:
        return _record(
            exc.status, exc.reason,
            evidence_scope="local_git_openpgp_signatures",
            commit_object_id=commit_oid,
            tag_name=normalized_tag,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _record(
            "unavailable", "local_verification_unavailable",
            evidence_scope="local_git_openpgp_signatures",
            commit_object_id=commit_oid,
            tag_name=normalized_tag,
        )


def _positive_identifier(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _normalize_release_identity(identity: Any) -> dict[str, Any]:
    required = {"repository_id", "repository_name", "release_id", "tag_name", "source_ref", "source_commit"}
    if not isinstance(identity, Mapping) or set(identity) != required:
        raise _VerificationFailure("incomplete", "release_identity_incomplete")
    repository_id = identity.get("repository_id")
    repository_name = identity.get("repository_name")
    release_id = identity.get("release_id")
    if not _positive_identifier(repository_id) or not _positive_identifier(release_id):
        raise _VerificationFailure("invalid", "release_identity_invalid")
    if (
        not isinstance(repository_name, str)
        or len(repository_name) > 512
        or not _REPOSITORY_NAME.fullmatch(repository_name)
        or any(part in {".", ".."} for part in repository_name.split("/"))
    ):
        raise _VerificationFailure("invalid", "release_identity_invalid")
    tag_name = _validate_tag_name(identity.get("tag_name"))
    source_ref = identity.get("source_ref")
    source_commit = identity.get("source_commit")
    if source_ref != f"refs/tags/{tag_name}" or not isinstance(source_commit, str) or not _OBJECT_ID.fullmatch(source_commit):
        raise _VerificationFailure("invalid", "release_source_identity_invalid")
    return {
        "repository_id": repository_id,
        "repository_name": repository_name,
        "release_id": release_id,
        "tag_name": tag_name,
        "source_ref": source_ref,
        "source_commit": source_commit,
    }


def _hash_artifact(root: Path, relative_path: str, max_bytes: int) -> tuple[str, int]:
    try:
        _normalized, parts = local_evidence._relative_parts(relative_path)
        descriptor = local_evidence._open_beneath(root, parts)
    except local_evidence._EvidenceFailure as exc:
        status = "invalid" if exc.reason in {
            "asset_path_invalid", "symlink_not_allowed", "regular_file_required",
            "path_component_not_directory", "file_size_limit_exceeded", "safe_file_open_unavailable",
        } else "unavailable"
        raise _VerificationFailure(status, exc.reason) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _VerificationFailure("invalid", "regular_file_required")
        if before.st_size < 0 or before.st_size > max_bytes:
            raise _VerificationFailure("invalid", "artifact_size_limit_exceeded")
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            except OSError as exc:
                raise _VerificationFailure("unavailable", "artifact_read_failed") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _VerificationFailure("invalid", "artifact_size_limit_exceeded")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ) or total != after.st_size:
            raise _VerificationFailure("invalid", "artifact_changed_during_read")
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def verify_release_artifacts(
    checkout_path: str | os.PathLike[str],
    release_identity: Mapping[str, Any] | None,
    artifacts: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Hash caller-selected local files and bind each result to supplied release identity."""
    try:
        identity = _normalize_release_identity(release_identity)
        if artifacts is None:
            raise _VerificationFailure("incomplete", "artifact_expectations_missing")
        if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
            raise _VerificationFailure("invalid", "artifact_expectations_invalid")
        if len(artifacts) == 0:
            raise _VerificationFailure("incomplete", "artifact_expectations_missing")
        if len(artifacts) > _MAX_ARTIFACTS:
            raise _VerificationFailure("invalid", "artifact_expectations_invalid")
        root = _resolve_checkout(checkout_path)
        git = _discover_executable("git", root)
        if git is None:
            raise _VerificationFailure("unavailable", "required_tool_unavailable")
        _validate_checkout(root, git)
        _check_tag_ref(git, root, identity["tag_name"])
        tag_commit = _git(git, root, ["rev-parse", "--verify", "--end-of-options", f"refs/tags/{identity['tag_name']}^{{}}"])
        if tag_commit.decode("ascii", errors="strict") != identity["source_commit"]:
            raise _VerificationFailure("invalid", "release_tag_source_commit_mismatch")
        results: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        seen_asset_ids: set[int] = set()
        total_bytes = 0
        for item in artifacts:
            if not isinstance(item, Mapping) or set(item) != {"asset_id", "name", "relative_path", "expected_sha256"}:
                results.append(_record("invalid", "artifact_expectation_invalid", asset_id=None, name=None, relative_path=None))
                continue
            asset_id = item.get("asset_id")
            name = item.get("name")
            relative_path = item.get("relative_path")
            expected_digest = item.get("expected_sha256")
            if (
                not _positive_identifier(asset_id)
                or not isinstance(name, str)
                or not name
                or len(name) > 255
                or any(ord(ch) < 32 for ch in name)
                or not isinstance(relative_path, str)
                or not isinstance(expected_digest, str)
                or not _SHA256.fullmatch(expected_digest)
            ):
                results.append(_record("invalid", "artifact_expectation_invalid", asset_id=asset_id if _positive_identifier(asset_id) else None, name=name if isinstance(name, str) else None, relative_path=None))
                continue
            try:
                normalized, _parts = local_evidence._relative_parts(relative_path)
            except local_evidence._EvidenceFailure as exc:
                results.append(_record("invalid", exc.reason, asset_id=asset_id, name=name, relative_path=None))
                continue
            if normalized in seen_paths:
                results.append(_record("invalid", "duplicate_artifact_path", asset_id=asset_id, name=name, relative_path=normalized))
                continue
            if asset_id in seen_asset_ids:
                results.append(_record("invalid", "duplicate_asset_id", asset_id=asset_id, name=name, relative_path=normalized))
                continue
            seen_paths.add(normalized)
            seen_asset_ids.add(asset_id)
            try:
                remaining_total = _MAX_TOTAL_ARTIFACT_BYTES - total_bytes
                digest, size = _hash_artifact(root, normalized, min(_MAX_ARTIFACT_BYTES, remaining_total))
                total_bytes += size
                if total_bytes > _MAX_TOTAL_ARTIFACT_BYTES:
                    raise _VerificationFailure("invalid", "total_artifact_size_limit_exceeded")
                if digest != expected_digest:
                    results.append(_record("invalid", "artifact_digest_mismatch", asset_id=asset_id, name=name, relative_path=normalized))
                else:
                    results.append(_record("matched", None, asset_id=asset_id, name=name, relative_path=normalized, sha256=digest, size_bytes=size))
            except _VerificationFailure as exc:
                results.append(_record(exc.status, exc.reason, asset_id=asset_id, name=name, relative_path=normalized))
        statuses = {item["status"] for item in results}
        if statuses == {"matched"}:
            status, reason = "incomplete", "independent_release_proof_missing"
        elif "invalid" in statuses:
            status, reason = "invalid", "one_or_more_artifacts_invalid"
        elif "unavailable" in statuses:
            status, reason = "unavailable", "one_or_more_artifacts_unavailable"
        else:
            status, reason = "incomplete", "artifact_expectations_incomplete"
        return _record(
            status, reason,
            evidence_scope="local_file_digest_match_only",
            release_identity=identity,
            artifacts=results,
        )
    except _VerificationFailure as exc:
        return _record(
            exc.status, exc.reason,
            evidence_scope="local_file_digest_match_only",
            release_identity=None,
            artifacts=[],
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _record(
            "unavailable", "local_verification_unavailable",
            evidence_scope="local_file_digest_match_only",
            release_identity=None,
            artifacts=[],
        )

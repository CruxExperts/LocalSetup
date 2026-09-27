"""Safe, read-only evidence from a local Git checkout."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from ..git_subprocess import git_subprocess_env
from .model import RepositoryTarget
from .policy import PolicyError, parse_target


_MAX_OUTPUT_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 5.0
_SNAPSHOT_TIMEOUT_SECONDS = 20.0
_HEX_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCP_REMOTE = re.compile(r"^(?:[^/@:\s]+@)?([^:/\s]+):(.+)$")
_SAFE_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_SCHEMES = {"git", "http", "https", "ssh"}
_READ_ONLY_GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")


class _CheckoutFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _clean_git_env() -> dict[str, str]:
    env = git_subprocess_env()
    for name in tuple(env):
        if name in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _run_git(root: Path, args: list[str], deadline: float) -> tuple[bytes, int]:
    """Run one fixed local Git read while bounding elapsed time and captured bytes."""
    if shutil.which("git") is None:
        raise _CheckoutFailure("git_unavailable")

    command = ["git", "--no-replace-objects", *_READ_ONLY_GIT_CONFIG, *args]
    remaining = min(_COMMAND_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        raise _CheckoutFailure("git_timeout")
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=_clean_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise _CheckoutFailure("git_unavailable") from exc
    except OSError as exc:
        raise _CheckoutFailure("git_unavailable") from exc

    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    captured = 0
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    end = time.monotonic() + remaining
    try:
        while selector.get_map():
            wait = end - time.monotonic()
            if wait <= 0:
                raise _CheckoutFailure("git_timeout")
            ready = selector.select(wait)
            if not ready:
                raise _CheckoutFailure("git_timeout")
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), min(65536, _MAX_OUTPUT_BYTES + 1 - captured))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > _MAX_OUTPUT_BYTES:
                    raise _CheckoutFailure("git_output_limit_exceeded")
                if key.data == "stdout":
                    output.extend(chunk)
        returncode = process.wait(timeout=max(0.01, end - time.monotonic()))
        return bytes(output), returncode
    except subprocess.TimeoutExpired as exc:
        raise _CheckoutFailure("git_timeout") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        process.stdout.close()
        process.stderr.close()


def _required_output(root: Path, args: list[str], deadline: float) -> bytes:
    output, code = _run_git(root, args, deadline)
    if code != 0:
        raise _CheckoutFailure("invalid_git_state")
    return output


def _single_line(output: bytes) -> bytes:
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise _CheckoutFailure("invalid_git_state")
    return output[:-1]


def _root_binding(root: Path) -> str:
    canonical = os.path.normcase(os.path.normpath(os.fspath(root.resolve(strict=True))))
    return hashlib.sha256(b"localsetup-github-checkout-root\0" + os.fsencode(canonical)).hexdigest()


def _status_counts(status: bytes) -> tuple[int, int, int]:
    if status and not status.endswith(b"\0"):
        raise _CheckoutFailure("invalid_git_state")
    staged = 0
    worktree = 0
    untracked = 0
    records = status.split(b"\0")
    index = 0
    allowed = set(b" MADRCTU?!")
    while index < len(records) - 1:
        record = records[index]
        if len(record) < 4 or record[2] != ord(" ") or record[0] not in allowed or record[1] not in allowed:
            raise _CheckoutFailure("invalid_git_state")
        x, y = record[0], record[1]
        if x == ord("?") and y == ord("?"):
            untracked += 1
        else:
            if x != ord(" "):
                staged += 1
            if y != ord(" "):
                worktree += 1
        if x in (ord("R"), ord("C")) or y in (ord("R"), ord("C")):
            if index + 1 >= len(records) - 1:
                raise _CheckoutFailure("invalid_git_state")
            index += 2
        else:
            index += 1
    return staged, worktree, untracked


def _decode_field(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _CheckoutFailure("invalid_git_state") from exc


def _remote_parts(remote: str) -> tuple[str, str, str] | None:
    value = remote.strip()
    if not value or "\x00" in value:
        return None

    if "://" in value:
        try:
            parsed = urlsplit(value)
            scheme = parsed.scheme.lower()
            if scheme not in _ALLOWED_SCHEMES or parsed.hostname is None or parsed.query or parsed.fragment:
                return None
            port = parsed.port
            default_ports = {"git": 9418, "http": 80, "https": 443, "ssh": 22}
            if port is not None and port != default_ports[scheme]:
                return None
            host, path = parsed.hostname, parsed.path
        except ValueError:
            return None
    else:
        match = _SCP_REMOTE.fullmatch(value)
        if match is None:
            return None
        host, path = match.groups()

    try:
        normalized_host = parse_target(host, "Owner/Repo").hostname
    except (PolicyError, AttributeError):
        return None
    parts = [unquote(piece) for piece in path.strip("/").split("/")]
    if len(parts) != 2:
        return None
    repository = parts[1]
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not parts[0] or not repository:
        return None
    return normalized_host, parts[0], repository


def _remote_matches_target(remote: str, target: RepositoryTarget) -> bool:
    parts = _remote_parts(remote)
    return bool(
        parts
        and parts[0] == target.hostname
        and parts[1].casefold() == target.owner.casefold()
        and parts[2].casefold() == target.repository.casefold()
    )


def _remote_urls(root: Path, remote_name: str, deadline: float) -> list[str]:
    key = f"remote.{remote_name}.url"
    output, code = _run_git(root, ["config", "--local", "--null", "--get-all", key], deadline)
    if code == 1:
        return []
    if code != 0 or not output.endswith(b"\0"):
        raise _CheckoutFailure("invalid_git_state")
    try:
        values = [_decode_field(item) for item in output[:-1].split(b"\0")]
    except _CheckoutFailure:
        raise
    if not values or any(not item for item in values):
        raise _CheckoutFailure("invalid_git_state")
    return values


def _validate_target(target: Any) -> RepositoryTarget:
    if not isinstance(target, RepositoryTarget):
        raise _CheckoutFailure("invalid_repository_target")
    try:
        return parse_target(target.hostname, target.full_name)
    except (PolicyError, AttributeError, TypeError) as exc:
        raise _CheckoutFailure("invalid_repository_target") from exc


def checkout_is_bound_to_target(checkout: Any) -> bool:
    """Require an established matching fetch remote before using local evidence or applying."""
    if not isinstance(checkout, dict) or checkout.get("supported") is not True:
        return False
    origin = checkout.get("origin")
    upstream = checkout.get("upstream")
    if not isinstance(origin, dict) or not isinstance(upstream, dict):
        return False
    configured = [remote for remote in (origin, upstream) if remote.get("configured") is True]
    return bool(configured) and all(remote.get("matches_target") is True for remote in configured)


def snapshot_checkout(checkout_path: str | os.PathLike[str], target: RepositoryTarget) -> dict[str, Any]:
    """Return safe local checkout evidence without changing the checkout or using network access.

    File names, checkout paths, and raw remote URLs are kept out of the returned
    structure. A SHA-256 root binding identifies the resolved checkout locally,
    and the dirty-state digest covers the exact NUL-delimited porcelain output.
    """
    try:
        normalized_target = _validate_target(target)
    except _CheckoutFailure as exc:
        return {"supported": False, "reason": exc.reason}

    if not isinstance(checkout_path, (str, os.PathLike)):
        return {"supported": False, "reason": "invalid_checkout_path"}
    try:
        requested_path = Path(checkout_path).expanduser().resolve(strict=True)
    except FileNotFoundError:
        return {"supported": False, "reason": "checkout_missing"}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {"supported": False, "reason": "invalid_checkout_path"}
    if not requested_path.is_dir():
        return {"supported": False, "reason": "checkout_not_directory"}

    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    try:
        root_bytes, root_code = _run_git(requested_path, ["rev-parse", "--show-toplevel"], deadline)
        if root_code != 0:
            return {"supported": False, "reason": "not_git_repository"}
        root_text = _single_line(root_bytes)
        if not root_text:
            raise _CheckoutFailure("invalid_git_state")
        root = Path(os.fsdecode(root_text)).resolve(strict=True)
        if not root.is_dir():
            raise _CheckoutFailure("invalid_git_state")

        head_bytes = _single_line(_required_output(root, ["rev-parse", "--verify", "HEAD"], deadline))
        head = _decode_field(head_bytes)
        if not _HEX_OBJECT_ID.fullmatch(head):
            raise _CheckoutFailure("invalid_git_state")

        branch_output, branch_code = _run_git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline)
        if branch_code == 0:
            branch = _decode_field(_single_line(branch_output))
            if not branch:
                raise _CheckoutFailure("invalid_git_state")
            detached = False
        elif branch_code == 1:
            branch = None
            detached = True
        else:
            raise _CheckoutFailure("invalid_git_state")

        status = _required_output(
            root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            deadline,
        )
        staged_count, worktree_count, untracked_count = _status_counts(status)

        upstream_remote: str | None = None
        upstream_branch: str | None = None
        if not detached:
            branch_ref = f"refs/heads/{branch}"
            upstream_output = _required_output(
                root,
                ["for-each-ref", "--format=%(upstream:remotename)%00%(upstream:remoteref)", branch_ref],
                deadline,
            )
            if upstream_output:
                upstream_row = _single_line(upstream_output)
                if b"\0" not in upstream_row:
                    raise _CheckoutFailure("invalid_git_state")
                remote_bytes, remote_ref_bytes = upstream_row.split(b"\0", 1)
                if remote_bytes or remote_ref_bytes:
                    if not remote_bytes or not remote_ref_bytes.startswith(b"refs/heads/"):
                        raise _CheckoutFailure("invalid_git_state")
                    upstream_remote = _decode_field(remote_bytes)
                    upstream_branch = _decode_field(remote_ref_bytes[len(b"refs/heads/"):])
                    if not upstream_remote or not upstream_branch:
                        raise _CheckoutFailure("invalid_git_state")

        origin_urls = _remote_urls(root, "origin", deadline)
        origin_matches = bool(origin_urls) and all(_remote_matches_target(url, normalized_target) for url in origin_urls)
        upstream_urls = _remote_urls(root, upstream_remote, deadline) if upstream_remote else []
        upstream_matches = bool(upstream_urls) and all(_remote_matches_target(url, normalized_target) for url in upstream_urls)
        safe_upstream_remote = upstream_remote if upstream_remote and _SAFE_REMOTE_NAME.fullmatch(upstream_remote) else None

        return {
            "supported": True,
            "reason": None,
            "target": normalized_target.full_name,
            "root_binding": _root_binding(root),
            "head": head,
            "branch": {"detached": detached, "name": branch},
            "dirty": {
                "clean": not (staged_count or worktree_count or untracked_count),
                "staged_count": staged_count,
                "worktree_count": worktree_count,
                "untracked_count": untracked_count,
                "status_digest": hashlib.sha256(status).hexdigest(),
            },
            "origin": {"configured": bool(origin_urls), "matches_target": origin_matches},
            "upstream": {
                "configured": upstream_remote is not None,
                "remote": safe_upstream_remote,
                "branch": upstream_branch,
                "matches_target": upstream_matches,
            },
        }
    except _CheckoutFailure as exc:
        return {"supported": False, "reason": exc.reason}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {"supported": False, "reason": "invalid_git_state"}

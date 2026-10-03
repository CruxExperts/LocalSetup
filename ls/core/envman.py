from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Literal

from .dependencies import tool_status
from .manifests import load_platforms


BOOTSTRAP_URL = "https://github.com/CruxExperts/envman/releases/latest/download/install.py"
PROBE_TIMEOUT_SECONDS = 10
GIT_TIMEOUT_SECONDS = 5
MUTATION_TIMEOUT_SECONDS = 900
VERSION_OUTPUT_LIMIT = 1024
CHECK_OUTPUT_LIMIT = 16 * 1024
GIT_OUTPUT_LIMIT = 8 * 1024

_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_VERSION_OUTPUT = re.compile(
    r"^(?:envman(?:, version)?\s+)?(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _CommandResult:
    returncode: int | None
    stdout: bytes = b""
    failure: Literal["launch", "timeout", "output-limit"] | None = None


def _run_capture(
    argv: list[str],
    *,
    cwd: Path | None,
    timeout: int,
    output_limit: int,
    env: dict[str, str] | None = None,
) -> _CommandResult:
    """Run a read-only command with bounded stdout and discarded stderr."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _CommandResult(None, failure="launch")

    if process.stdout is None:
        process.kill()
        process.wait()
        return _CommandResult(None, failure="launch")

    output = bytearray()
    overflow = threading.Event()

    def collect_stdout() -> None:
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                if len(output) + len(chunk) > output_limit:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
                output.extend(chunk)
        except OSError:
            return

    reader = threading.Thread(target=collect_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                process.kill()
            except OSError:
                pass
            break
        time.sleep(min(0.025, remaining))

    try:
        returncode = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        returncode = process.wait()
    reader.join(timeout=1)
    if overflow.is_set():
        return _CommandResult(returncode, failure="output-limit")
    if timed_out:
        return _CommandResult(returncode, failure="timeout")
    return _CommandResult(returncode, bytes(output))


def _run_discard(
    argv: list[str], *, cwd: Path | None, timeout: float
) -> _CommandResult:
    """Run an explicit mutation with both output streams discarded."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _CommandResult(None, failure="launch")
    try:
        return _CommandResult(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # This process owns the isolated group leader; still stop and reap it
            # if the group signal is unavailable on an unusual POSIX host.
            try:
                process.kill()
            except OSError:
                pass
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # SIGKILL cannot be ignored. Wait for the group leader to exit so it
            # is reaped before returning an uncertain mutation result.
            returncode = process.wait()
        return _CommandResult(returncode, failure="timeout")
    except OSError:
        return _CommandResult(None, failure="launch")


def _find_executable(name: str) -> str | None:
    try:
        result = tool_status(name)
    except Exception:
        return None
    path = result.get("path") if result.get("ok") else None
    return path if isinstance(path, str) and path else None


def _decode_json_object(payload: bytes) -> dict | None:
    try:
        text = payload.decode("utf-8", errors="strict")

        def unique_object(pairs: list[tuple[str, object]]) -> dict:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value

        decoded = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _semver(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _SEMVER.fullmatch(value)
    if match is None:
        return False
    prerelease = match.group(4)
    if prerelease:
        for identifier in prerelease.split("."):
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                return False
    return True


def _parse_version_output(payload: bytes) -> str | None:
    try:
        text = payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    match = _VERSION_OUTPUT.fullmatch(text)
    version = match.group("version") if match else None
    return version if _semver(version) else None


def _parse_check(payload: bytes) -> dict | None:
    value = _decode_json_object(payload)
    if value is None:
        return None
    keys = set(value)
    legacy_keys = {"target", "variables"}
    current_keys = legacy_keys | {"legacy_plaintext_snapshots", "storage_encrypted"}
    if keys not in (legacy_keys, current_keys):
        return None
    if not isinstance(value.get("target"), str):
        return None
    variables = value.get("variables")
    if type(variables) is not int or variables < 0:
        return None
    result: dict[str, object] = {"variables": variables}
    if keys == current_keys:
        snapshots = value.get("legacy_plaintext_snapshots")
        encrypted = value.get("storage_encrypted")
        if type(snapshots) is not int or snapshots < 0 or type(encrypted) is not bool:
            return None
        result["legacy_plaintext_snapshots"] = snapshots
        result["storage_encrypted"] = encrypted
    else:
        result["legacy_plaintext_snapshots"] = None
        result["storage_encrypted"] = None
    # `target` is a local path and is intentionally never returned.
    return result


def probe_status() -> dict:
    """Return a redacted, read-only Envman capability report."""
    executable = _find_executable("envman")
    if executable is None:
        return {"tool": "envman", "status": "unavailable", "code": "executable-missing"}

    version_result = _run_capture(
        [executable, "--version"],
        cwd=None,
        timeout=PROBE_TIMEOUT_SECONDS,
        output_limit=VERSION_OUTPUT_LIMIT,
    )
    check_result = _run_capture(
        [executable, "check", "--json"],
        cwd=None,
        timeout=PROBE_TIMEOUT_SECONDS,
        output_limit=CHECK_OUTPUT_LIMIT,
    )
    version = (
        _parse_version_output(version_result.stdout)
        if version_result.failure is None and version_result.returncode == 0
        else None
    )
    health = (
        _parse_check(check_result.stdout)
        if check_result.failure is None and check_result.returncode == 0
        else None
    )

    if version is None:
        return {"tool": "envman", "status": "present-unverified", "code": "version-unverified"}
    if health is None:
        return {
            "tool": "envman",
            "status": "present-unverified",
            "version": version,
            "code": _probe_failure_code(check_result, "invalid-response"),
        }

    result: dict[str, object] = {"tool": "envman", "version": version, **health}
    if health["storage_encrypted"] is False:
        result.update(status="present-unverified", code="storage-not-encrypted")
    elif int(health["legacy_plaintext_snapshots"] or 0) > 0:
        result.update(status="present-unverified", code="plaintext-snapshots-present")
    elif health["variables"] == 0:
        result.update(status="not-activated-or-empty", code=None)
    else:
        result.update(status="available", code=None)
    return result


def _probe_failure_code(result: _CommandResult, fallback: str) -> str:
    if result.failure == "timeout":
        return "probe-timeout"
    if result.failure == "output-limit":
        return "probe-output-limit"
    if result.failure == "launch":
        return "probe-launch-failed"
    if result.returncode != 0:
        return "probe-failed"
    return fallback


def _parse_update_check(payload: bytes) -> dict | None:
    value = _decode_json_object(payload)
    if value is None:
        return None
    if set(value) != {
        "schema",
        "schema_version",
        "status",
        "installed_version",
        "available_version",
    }:
        return None
    if value.get("schema") != "envman.update-result":
        return None
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return None
    status = value.get("status")
    if not isinstance(status, str) or status not in {"current", "update-available"}:
        return None
    if not _semver(value.get("installed_version")) or not _semver(value.get("available_version")):
        return None
    return {
        "status": value["status"],
        "installed_version": value["installed_version"],
        "available_version": value["available_version"],
    }


def update_check() -> tuple[dict, int]:
    executable = _find_executable("envman")
    if executable is None:
        return ({"tool": "envman", "status": "unavailable", "code": "executable-missing"}, 1)
    command = [executable, "update", "--check", "--json"]
    result = _run_capture(
        command,
        cwd=None,
        timeout=PROBE_TIMEOUT_SECONDS,
        output_limit=CHECK_OUTPUT_LIMIT,
    )
    parsed = (
        _parse_update_check(result.stdout)
        if result.failure is None and result.returncode == 0
        else None
    )
    if parsed is None:
        return (
            {
                "tool": "envman",
                "status": "present-unverified",
                "code": _probe_failure_code(result, "invalid-response"),
            },
            1,
        )
    return ({"tool": "envman", **parsed, "code": None}, 0)


def _git_root(directory: Path) -> Path | None:
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = _run_capture(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
        cwd=directory,
        timeout=GIT_TIMEOUT_SECONDS,
        output_limit=GIT_OUTPUT_LIMIT,
        env=git_env,
    )
    if result.failure is not None or result.returncode != 0:
        return None
    raw = result.stdout
    if not raw.endswith(b"\n"):
        return None
    raw = raw[:-1]
    if not raw or b"\n" in raw or b"\r" in raw or b"\0" in raw:
        return None
    try:
        root = Path(os.fsdecode(raw)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return root if root.is_dir() else None


def _skill_target(args, source_root: Path) -> tuple[str | None, Path | None, str | None]:
    install_skill = bool(getattr(args, "install_skill", False))
    scopes = getattr(args, "skill_scope", None) or []
    targets = getattr(args, "skill_target", None) or []
    raw_directory = getattr(args, "target_directory", None)
    if not install_skill:
        if scopes or targets or raw_directory:
            return None, None, "skill-options-require-install-skill"
        return None, None, None
    if len(scopes) != 1 or len(targets) != 1:
        return None, None, "skill-selection-required"
    scope = scopes[0]
    target = targets[0]
    try:
        supported_targets = {item.platform_id for item in load_platforms(source_root)}
    except Exception:
        return None, None, "platform-targets-unavailable"
    if not isinstance(target, str) or target not in supported_targets:
        return None, None, "unsupported-skill-target"

    if scope == "global":
        if raw_directory:
            return None, None, "global-target-not-allowed"
        return scope, None, None
    if scope != "repository":
        return None, None, "unsupported-skill-scope"
    if not isinstance(raw_directory, str) or not raw_directory:
        return None, None, "repository-target-required"
    if any(character in raw_directory for character in ("\0", "\n", "\r")):
        return None, None, "repository-target-invalid"
    try:
        directory = Path(raw_directory).expanduser().resolve(strict=True)
        if not directory.is_dir() or _git_root(directory) != directory:
            return None, None, "repository-root-required"
    except (OSError, RuntimeError, ValueError):
        return None, None, "repository-root-required"
    return scope, directory, None


def _supported_bootstrap_host() -> bool:
    return sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"}


def mutate(args, source_root: Path) -> tuple[dict, int]:
    action = args.envman_action
    if bool(getattr(args, "check", False)):
        return ({"tool": "envman", "status": "invalid-request", "code": "check-not-supported"}, 2)

    scope, cwd, selection_error = _skill_target(args, source_root)
    if selection_error:
        return (
            {"tool": "envman", "action": action, "status": "not-started", "code": selection_error},
            2,
        )
    if not _supported_bootstrap_host():
        return (
            {"tool": "envman", "action": action, "status": "not-started", "code": "unsupported-host"},
            2,
        )
    uv = _find_executable("uv")
    if uv is None:
        return ({"tool": "envman", "action": action, "status": "not-started", "code": "uv-unavailable"}, 2)

    command = [
        uv,
        "run",
        "--python",
        "3.12",
        "--script",
        BOOTSTRAP_URL,
    ]
    if scope is not None:
        command.extend(["--install-skill", "--skill-scope", scope, "--skill-target", args.skill_target[0]])
    result = _run_discard(command, cwd=cwd, timeout=MUTATION_TIMEOUT_SECONDS)
    if result.failure == "timeout":
        code = "mutation-timeout-unknown"
    elif result.failure is not None or result.returncode != 0:
        code = "mutation-failed-unknown"
    else:
        code = None
    return (
        {
            "tool": "envman",
            "action": action,
            "status": "completed" if code is None else "unknown",
            "code": code,
        },
        0 if code is None else 1,
    )


def handle(args, source_root: Path) -> int:
    try:
        if args.envman_action == "status":
            payload = probe_status()
            code = 0
        elif args.envman_action == "update" and bool(getattr(args, "check", False)):
            if (
                bool(getattr(args, "install_skill", False))
                or getattr(args, "skill_scope", None)
                or getattr(args, "skill_target", None)
                or getattr(args, "target_directory", None)
            ):
                payload, code = (
                    {"tool": "envman", "status": "invalid-request", "code": "check-cannot-install-skill"},
                    2,
                )
            else:
                payload, code = update_check()
        else:
            payload, code = mutate(args, source_root)
    except Exception:
        action = getattr(args, "envman_action", "unknown")
        if action == "status":
            payload, code = (
                {"tool": "envman", "status": "present-unverified", "code": "probe-failed"},
                0,
            )
        elif action == "update" and bool(getattr(args, "check", False)):
            payload, code = (
                {"tool": "envman", "status": "present-unverified", "code": "probe-failed"},
                1,
            )
        else:
            payload, code = (
                {"tool": "envman", "action": action, "status": "unknown", "code": "mutation-failed-unknown"},
                1,
            )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code

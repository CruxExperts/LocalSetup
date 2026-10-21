"""Target-pane sudo probe and private environment-credential check."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time


def _safe_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.endswith("_SUDO_KEY"):
            env.pop(name, None)
    return env


def _run_sudo(args: list[str], *, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        env=_safe_environment(),
        check=False,
    )


def _output_text(result: subprocess.CompletedProcess[bytes]) -> str:
    return ((result.stdout or b"") + b"\n" + (result.stderr or b"")).decode("utf-8", "replace").lower()


def _password_required(output: str) -> bool:
    return bool(
        re.search(
            r"password.*required|a password is required|terminal is required|authentication required|a terminal is required",
            output,
            re.I,
        )
    )


def _unsupported_noninteractive_option(output: str) -> bool:
    return bool(re.search(r"invalid option|illegal option|unknown option|usage: sudo", output, re.I))


def _definite_rejection(output: str) -> bool:
    # Only sudo's explicit retry response is sufficient to try the next key.
    return "sorry, try again" in output.lower()


def _candidate_names(hostname: str) -> list[str]:
    names = []
    if hostname and re.fullmatch(r"[A-Za-z0-9._-]+", hostname):
        names.append(hostname.upper() + "_SUDO_KEY")
    names.append("GENERIC_SUDO_KEY")
    return list(dict.fromkeys(names))


def _write_result(payload: dict[str, object]) -> None:
    result_path = os.environ["TMUX_PROBE_RESULT"]
    temp_path = result_path + ".tmp"
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as result_file:
        json.dump(payload, result_file, sort_keys=True)
        result_file.write("\n")
    os.replace(temp_path, result_path)
    subprocess.run(
        ["tmux", "wait-for", "-S", os.environ["TMUX_PROBE_CHANNEL"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _probe() -> dict[str, object]:
    operation_id = os.environ["TMUX_PROBE_OPERATION_ID"]
    payload: dict[str, object] = {
        "operation_id": operation_id,
        "session": os.environ["TMUX_PROBE_SESSION"],
        "pane_id": os.environ.get("TMUX_PROBE_PANE_ID") or None,
        "pane_tty": os.environ.get("TMUX_PROBE_PANE_TTY") or None,
        "checked_at": time.time(),
        "sudo": "failed",
        "detail": "sudo probe failed safely; refusing credential fallback",
        "probe_command": "sudo -Nnv",
        "credential_outcome": "not_checked",
        "credential_variable": None,
        "credential_candidates": [],
        "credential_candidates_checked": False,
    }

    try:
        initial = _run_sudo(["sudo", "-Nnv"])
    except subprocess.TimeoutExpired:
        payload["detail"] = "sudo probe timed out; refusing credential fallback"
        return payload
    except OSError:
        payload["detail"] = "sudo probe could not be reconciled; refusing credential fallback"
        return payload

    output = _output_text(initial)
    if initial.returncode == 0:
        payload.update({"sudo": "ready", "detail": "", "credential_outcome": "cached"})
        return payload
    if _unsupported_noninteractive_option(output):
        payload["probe_command"] = "sudo -vn"
        try:
            initial = _run_sudo(["sudo", "-vn"])
        except subprocess.TimeoutExpired:
            payload["detail"] = "sudo probe timed out; refusing credential fallback"
            return payload
        except OSError:
            payload["detail"] = "sudo probe could not be reconciled; refusing credential fallback"
            return payload
        output = _output_text(initial)
        if initial.returncode == 0:
            payload.update({"sudo": "ready", "detail": "", "credential_outcome": "cached"})
            return payload

    if not _password_required(output):
        payload["detail"] = "sudo is unavailable, denied, or blocked by policy"
        return payload

    if os.environ.get("TMUX_PROBE_ALLOW_CREDENTIALS") != "1":
        payload.update(
            {
                "sudo": "password_required",
                "detail": "manual sudo validation is required; automatic credentials are disabled for this operation",
                "credential_outcome": "manual",
            }
        )
        return payload

    candidates: list[dict[str, str]] = []
    payload["credential_candidates_checked"] = True
    for variable in _candidate_names(os.environ.get("TMUX_PROBE_HOSTNAME", "").strip()):
        if variable not in os.environ:
            candidates.append({"variable": variable, "status": "unavailable"})
            continue
        secret = os.environ[variable]
        if secret == "":
            candidates.append({"variable": variable, "status": "empty"})
            continue
        if "\n" in secret or "\r" in secret:
            candidates.append({"variable": variable, "status": "rejected"})
            continue
        try:
            attempt = _run_sudo(["sudo", "-S", "-v", "-p", ""], input_data=(secret + "\n").encode("utf-8"))
        except subprocess.TimeoutExpired:
            candidates.append({"variable": variable, "status": "ambiguous"})
            payload.update(
                {
                    "detail": "sudo credential check timed out; refusing fallback",
                    "credential_outcome": "blocked",
                    "credential_candidates": candidates,
                }
            )
            return payload
        except OSError:
            candidates.append({"variable": variable, "status": "ambiguous"})
            payload.update(
                {
                    "detail": "sudo credential check could not be reconciled; refusing fallback",
                    "credential_outcome": "blocked",
                    "credential_candidates": candidates,
                }
            )
            return payload

        if attempt.returncode == 0:
            candidates.append({"variable": variable, "status": "used"})
            payload.update(
                {
                    "sudo": "ready",
                    "detail": "",
                    "credential_outcome": "automatic",
                    "credential_variable": variable,
                    "credential_candidates": candidates,
                }
            )
            return payload
        if _definite_rejection(_output_text(attempt)):
            candidates.append({"variable": variable, "status": "rejected"})
            continue

        candidates.append({"variable": variable, "status": "ambiguous"})
        payload.update(
            {
                "detail": "sudo credential result is ambiguous; refusing fallback",
                "credential_outcome": "blocked",
                "credential_candidates": candidates,
            }
        )
        return payload

    rejected = any(candidate["status"] == "rejected" for candidate in candidates)
    payload.update(
        {
            "sudo": "password_required",
            "detail": (
                "sudo credentials were rejected or unavailable; manual sudo validation is required"
                if rejected
                else "no non-empty sudo credential variables were available; manual sudo validation is required"
            ),
            "credential_outcome": "manual" if rejected else "no_env",
            "credential_candidates": candidates,
        }
    )
    return payload


def main() -> None:
    try:
        payload = _probe()
    except Exception:
        payload = {
            "operation_id": os.environ.get("TMUX_PROBE_OPERATION_ID"),
            "session": os.environ.get("TMUX_PROBE_SESSION"),
            "pane_id": os.environ.get("TMUX_PROBE_PANE_ID") or None,
            "pane_tty": os.environ.get("TMUX_PROBE_PANE_TTY") or None,
            "sudo": "failed",
            "detail": "sudo probe failed safely; refusing credential fallback",
            "probe_command": "sudo -Nnv",
            "credential_outcome": "blocked",
            "credential_variable": None,
            "credential_candidates": [],
            "credential_candidates_checked": False,
        }
    try:
        _write_result(payload)
    except Exception:
        # The pending operation remains fenced if its private result cannot be written.
        return


if __name__ == "__main__":
    main()

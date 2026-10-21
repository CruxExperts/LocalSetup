from __future__ import annotations

import os
import re
import shlex
import subprocess

from .constants import IDLE_PROMPT_RE, PASSWORD_PROMPT_RE, TmuxResult


def _tmux_cmd(args: list[str]) -> list[str]:
    base = shlex.split(os.environ.get("TMUX_OPS_TMUX", "tmux"))
    return base + args


def _run_tmux(args: list[str], timeout: float = 5.0) -> TmuxResult:
    try:
        result = subprocess.run(
            _tmux_cmd(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return TmuxResult(result.returncode, result.stdout or "", result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return TmuxResult(-1, "", f"tmux timeout after {exc.timeout}s: {exc.cmd}")
    except OSError as exc:
        return TmuxResult(-1, "", f"tmux execution failed: {type(exc).__name__}: {exc}")


def _start_tmux_wait(channel: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _tmux_cmd(["wait-for", channel]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_proc(proc: subprocess.Popen[str], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return False


def _session_list() -> tuple[set[str] | None, str | None]:
    result = _run_tmux(["list-sessions", "-F", "#{session_name}"])
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"tmux list-sessions exited {result.returncode}"
        detail_lower = detail.lower()
        if "no server running" in detail_lower or "error connecting to" in detail_lower:
            return set(), None
        return None, detail
    return {s.strip() for s in result.stdout.splitlines() if s.strip()}, None


def _session_exists(session: str) -> bool:
    result = _run_tmux(["has-session", "-t", session])
    return result.returncode == 0


def _pane_target(session: str) -> tuple[str | None, str | None]:
    result = _run_tmux(["list-panes", "-t", session, "-F", "#{pane_id}"])
    if result.returncode != 0:
        return None, result.stderr or f"tmux list-panes exited {result.returncode}"
    panes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(panes) != 1:
        return None, f"expected one exact pane for legacy session {session}; found {len(panes)}"
    return panes[0], None


def _pane_tty(session: str) -> tuple[str | None, str | None]:
    result = _targeted_tmux(session, ["display-message", "-t", "{target}", "-p", "-F", "#{pane_tty}"])
    if result.returncode != 0:
        return None, result.stderr or f"tmux display-message exited {result.returncode}"
    tty = result.stdout.strip()
    return (tty or None), None


def _targeted_tmux(session: str, args: list[str], timeout: float = 5.0) -> TmuxResult:
    pane, err = _pane_target(session)
    if pane is None:
        return TmuxResult(-1, "", err or f"unable to resolve pane target for {session}")
    owner = _run_tmux(["show-options", "-p", "-t", pane, "-v", "@localsetup_managed_ops"], timeout=timeout)
    if owner.returncode == 0 and owner.stdout.strip() == "1":
        return TmuxResult(-1, "", "managed operations panes are reserved for their controller-owned workflow")
    resolved_args = [pane if arg == "{target}" else arg for arg in args]
    result = _run_tmux(resolved_args, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr or result.stdout
        if detail:
            detail = f"{detail.rstrip()}\nresolved_target={pane}"
        else:
            detail = f"resolved_target={pane}"
        return TmuxResult(result.returncode, result.stdout, detail)
    return result


def _cursor_y(target: str) -> tuple[int | None, str | None]:
    result = _targeted_tmux(target, ["display-message", "-t", "{target}", "-p", "-F", "#{cursor_y}"])
    if result.returncode != 0:
        return None, result.stderr or f"tmux display-message exited {result.returncode}"
    try:
        return int(result.stdout.strip()), None
    except ValueError:
        return None, f"invalid cursor_y output: {result.stdout!r}"


def _capture_line(target: str, line_index: int) -> tuple[str, str | None]:
    result = _targeted_tmux(
        target,
        [
            "capture-pane",
            "-t",
            "{target}",
            "-p",
            "-S",
            str(line_index),
            "-E",
            str(line_index),
        ],
    )
    if result.returncode != 0:
        return "", result.stderr or f"tmux capture-pane exited {result.returncode}"
    return result.stdout.strip(), None


def _is_pane_idle(target: str, idle_re: re.Pattern[str] | None = None) -> bool:
    pattern = idle_re or IDLE_PROMPT_RE
    cursor_y, _ = _cursor_y(target)
    if cursor_y is None:
        return False
    line, _ = _capture_line(target, cursor_y)
    return bool(pattern.match(line))


def _is_pane_waiting_sudo(target: str) -> bool:
    cursor_y, _ = _cursor_y(target)
    if cursor_y is None:
        return False
    line, _ = _capture_line(target, cursor_y)
    return bool(PASSWORD_PROMPT_RE.search(line))


def _snapshot_cursor(target: str) -> int | None:
    cursor_y, _ = _cursor_y(target)
    return cursor_y

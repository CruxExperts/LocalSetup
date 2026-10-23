"""Bounded, literal activity canvas for the versioned family renderer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import signal
import time
import unicodedata
from typing import Any, Mapping, Sequence

from .command_state import ShellCommandError, atomic_json, process_info, read_json, session_lock


@dataclass(frozen=True)
class Section:
    operation_id: str
    title: str
    state: str
    activated: int
    last_output: float
    minimum: int = 10
    input_required: bool = False
    output: str = ""


@dataclass(frozen=True)
class Page:
    sections: tuple[Section, ...]
    heights: tuple[int, ...]


def pages(sections: Sequence[Section], height: int, *, quiet_seconds: float = 5,
          max_sections: int = 8, now: float | None = None) -> tuple[Page, ...]:
    """Pack without splitting; each collapsed section costs exactly one row."""
    if height < 1 or max_sections < 1:
        raise ValueError("invalid canvas geometry")
    moment = time.monotonic() if now is None else now
    newest = max(sections, key=lambda item: (item.activated, item.operation_id), default=None)
    expanded = lambda item: (item.input_required or item is newest or
                             item.state == "running" and moment - item.last_output < quiet_seconds)
    ordered = sorted(sections, key=lambda item: (expanded(item), item.activated, item.operation_id))
    if not ordered:
        return (Page((), ()),)
    packed: list[list[Section]] = [[]]
    costs: list[int] = [0]
    for section in ordered:
        if type(section.minimum) is not int or not 2 <= section.minimum <= 100:
            raise ValueError("invalid expanded minimum")
        minimum = section.minimum if expanded(section) else 1
        if packed[-1] and (len(packed[-1]) >= max_sections or costs[-1] + minimum > height):
            packed.append([])
            costs.append(0)
        packed[-1].append(section)
        costs[-1] += minimum
    result: list[Page] = []
    for group in packed:
        minima = [item.minimum if expanded(item) else 1 for item in group]
        spare = max(0, height - sum(minima))
        active = [index for index, item in enumerate(group) if expanded(item)]
        if active and spare:
            denominator = sum(minima[index] for index in active)
            shares = [(spare * minima[index] / denominator, index) for index in active]
            allocated = sum(math.floor(share) for share, _ in shares)
            for share, index in shares:
                minima[index] += math.floor(share)
            for _, index in sorted(shares, key=lambda entry: (-(entry[0] % 1), entry[1]))[:spare - allocated]:
                minima[index] += 1
        result.append(Page(tuple(group), tuple(minima)))
    return tuple(result)


def safe_text(value: str) -> str:
    """Render untrusted output literally; terminal escapes never reach the pane."""
    out: list[str] = []
    for char in value.replace("\r\n", "\n"):
        if char == "\t":
            out.append("    ")
        elif char in "\r\n":
            out.append("\n")
        elif unicodedata.category(char).startswith("C") or char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            out.append("?")
        else:
            out.append(char)
    return "".join(out)


def _clip(value: str, columns: int) -> str:
    cells = 0
    result: list[str] = []
    for char in safe_text(value).replace("\n", " "):
        width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if cells + width > columns:
            break
        result.append(char)
        cells += width
    return "".join(result)


def native_home_lines(session: Mapping[str, Any], *, columns: int, rows: int) -> tuple[str, ...]:
    """Fill an empty native workspace while leaving its last row for controls."""
    if columns < 1 or rows < 1:
        return ()
    body = [
        f"  LocalSetup   |   {session.get('host', '')}",
        "  " + "-" * max(0, columns - 4),
        "",
        "  Workspace ready",
        "  Commands appear here as they run. Their output stays available afterward.",
        "",
        "  F6  Open controls       Arrows + Enter  Choose       Escape  Return",
        "  Click the controls below to select panes, adjust layout, or read logs.",
        "",
        "  Follow   Bring active work into view.",
        "  Stable   Keep the panes you selected in view.",
        "  Fixed    Keep your chosen panes and their proportions.",
        "",
        "  Drag a divider to change the space assigned to a pane.",
        "  Page controls reveal additional work when the terminal fills up.",
    ]
    drawable = rows - 1
    return tuple(_clip(line, columns) for line in (body + [""] * drawable)[:drawable])


def render(page: Page, *, columns: int, rows: int, page_index: int, page_count: int) -> tuple[str, ...]:
    """Produce exactly the drawable rows and a dedicated navigation row."""
    footer = 1
    lines: list[str] = []
    if columns < 80:
        lines.append(_clip("Resize to at least 80 columns", columns))
    elif any(height > rows - footer for height in page.heights):
        lines.append(_clip("Increase terminal height to display this operation", columns))
    else:
        if not page.sections:
            lines.append(_clip("root · Idle", columns))
        for section, size in zip(page.sections, page.heights):
            state = "Input required" if section.input_required else section.state.title()
            lines.append(_clip(f"{section.title}  [{state}]", columns))
            if size == 1:
                continue
            content = [line for line in safe_text(section.output).splitlines()]
            content = content[-(size - 1):]
            lines.extend(_clip(line, columns) for line in content)
            lines.extend("" for _ in range(size - 1 - len(content)))
    lines.extend("" for _ in range(max(0, rows - footer - len(lines))))
    left = "Page left" if page_index else "Page left (end)"
    right = "Page right" if page_index + 1 < page_count else "Page right (end)"
    if rows == 1:
        return (_clip(f"{left} [ {page_index + 1}/{page_count} ] {right}", columns),)
    lines.append(_clip(f"{left} [ {page_index + 1}/{page_count} ] {right}", columns))
    return tuple(lines[:rows])


def _mouse_step(x: int, y: int, *, rows: int, page_index: int, page_count: int) -> int:
    """Only the two visible footer labels are navigation targets."""
    if y != rows - 1:
        return 0
    left = "Page left" if page_index else "Page left (end)"
    right = "Page right" if page_index + 1 < page_count else "Page right (end)"
    right_start = len(left) + len(f" [ {page_index + 1}/{page_count} ] ")
    if 0 <= x < len(left):
        return -1
    if right_start <= x < right_start + len(right):
        return 1
    return 0


def _sections(directory: Path, session: Mapping[str, Any], *, columns: int | None = None) -> list[Section]:
    from .command_service import ShellCommandService, _label_width, _pane_label
    service = ShellCommandService(directory.parents[3])
    result: list[Section] = []
    for op in service._strict_ops(directory, session):
        paths = service._paths(directory, str(op["operation_id"]))
        cursor = {}
        try:
            cursor = read_json(paths["cursor"], limit=4096)
        except (OSError, ValueError, ShellCommandError):
            pass
        lease = op.get("input_lease")
        required = isinstance(lease, dict) and not lease.get("revoked") and time.monotonic() < lease.get("deadline_monotonic", 0)
        state = str(op.get("state", "unknown"))
        state_label = "Input required" if required else state.title()
        member_label = str(op.get("member_label", "root"))
        title = _pane_label(member_label.removeprefix("subagent "), str(op["operation_id"]),
                            str(op["cwd"]), root=member_label == "root",
                            width=max(1, (columns or int(session.get("canvas_width", 120))) -
                                      _label_width(f"  [{state_label}]")))
        result.append(Section(str(op["operation_id"]), title,
                              state, int(op.get("activation_seq", 0)),
                              float(cursor.get("activity_monotonic", op.get("activated_monotonic", 0))),
                              int(op.get("expanded_min_rows", session["runtime_settings"]["layout"]["expanded_min_rows"])),
                              required))
    return result


def _visible_output(directory: Path, session: Mapping[str, Any], page: Page) -> Page:
    """Read only displayed sections; pagination never scales reads with history."""
    from .command_service import ShellCommandService
    service = ShellCommandService(directory.parents[3])
    visible: list[Section] = []
    for item in page.sections:
        path = service._paths(directory, item.operation_id)["log"]
        output = ""
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                source.seek(max(0, source.tell() - 65536))
                output = source.read(65536).decode("utf-8", "replace")
        except OSError:
            pass
        visible.append(replace(item, output=output))
    return Page(tuple(visible), page.heights)


def _terminal_size() -> tuple[int, int]:
    size = os.get_terminal_size(1)
    return size.columns, size.lines


def selected_page(activity_pages: Sequence[Page], sections: Sequence[Section],
                  session: Mapping[str, Any]) -> int:
    """Follow the newest activation unless primary browsing selected an anchor."""
    newest = max(sections, key=lambda item: (item.activated, item.operation_id), default=None)
    anchor = newest.operation_id if session.get("canvas_follow_active", True) and newest else session.get("canvas_anchor")
    for index, page in enumerate(activity_pages):
        if any(item.operation_id == anchor for item in page.sections):
            return index
    predecessor = session.get("canvas_anchor_activation")
    if type(predecessor) is int:
        eligible = [(item.activated, index) for index, page in enumerate(activity_pages)
                    for item in page.sections if item.activated <= predecessor]
        if eligible:
            return max(eligible)[1]
    return min(max(0, int(session.get("canvas_page", 0))), len(activity_pages) - 1)


def page_step(session_record: Path, client_pid: int, step: int, *,
              mouse_x: int | None = None, mouse_y: int | None = None) -> bool:
    """Move the one verified primary viewer; secondary keys cannot change it."""
    mouse = mouse_x is not None or mouse_y is not None
    if ((step not in ({0} if mouse else {-1, 1})) or client_pid <= 0
            or (mouse and (type(mouse_x) is not int or type(mouse_y) is not int))):
        return False
    from .command_backend import TmuxCommandBackend
    from .command_service import ShellCommandService
    directory = session_record.parent
    service = ShellCommandService(directory.parents[3])
    with session_lock(directory, blocking=True):
        session = read_json(session_record, limit=2_000_000)
        if session.get("layout_mode") != "canvas-v2" or session.get("state") != "ready":
            return False
        pins, unknown = service._pins(directory, remove_dead=False)
        primaries = [pin for pin in pins if pin.get("primary_viewport") is True]
        primary = [pin for pin in primaries if pin.get("viewer_pid") == client_pid]
        if unknown or len(primaries) != 1 or len(primary) != 1:
            return False
        parent = process_info(primary[0].get("pid"))
        client = process_info(client_pid)
        if (parent is None or parent.get("starttime") != primary[0].get("starttime") or
                client is None or client.get("ppid") != primary[0].get("pid")):
            return False
        backend = TmuxCommandBackend()
        clients = backend._clients(session)
        if len([item for item in clients if item.get("pid") == client_pid and
                "ignore-size" in item.get("flags", set()) and
                "read-only" not in item.get("flags", set())]) != 1:
            return False
        if service._verify(session).get("state") != "ready":
            return False
        backend.verify_observer_keys(session)
        sections = _sections(directory, session)
        height = session.get("primary_viewport_height", 24)
        layout = session["runtime_settings"]["layout"]
        activity_pages = pages(sections, max(1, height - 1),
                               quiet_seconds=layout["quiet_seconds"],
                               max_sections=layout["max_sections"])
        index = selected_page(activity_pages, sections, session)
        if mouse:
            step = _mouse_step(mouse_x, mouse_y, rows=height,
                               page_index=index, page_count=len(activity_pages))
            if step == 0:
                return True
        target = min(max(0, index + step), len(activity_pages) - 1)
        if target == index:
            return True
        anchor = activity_pages[target].sections[0]
        newest = max(sections, key=lambda item: (item.activated, item.operation_id), default=None)
        session.update(canvas_anchor=anchor.operation_id,
                       canvas_anchor_activation=anchor.activated,
                       canvas_follow_active=bool(newest and newest in activity_pages[target].sections),
                       canvas_page=target)
        atomic_json(session_record, session)
    return True


def run_renderer(session_record: Path) -> int:
    """The sole tmux pane is a read-only renderer, never a command PTY."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    last: tuple[str, ...] = ()
    last_size: tuple[int, int] | None = None
    interval = .25
    while True:
        try:
            session = read_json(session_record, limit=2_000_000)
            if session.get("layout_mode") != "canvas-v2" or session.get("state") in {"removed", "closed"}:
                return 0
            directory = session_record.parent
            width, height = _terminal_size()
            layout = session["runtime_settings"]["layout"]
            interval = layout.get("activity_coalesce_ms", 250) / 1000
            sections = _sections(directory, session, columns=width)
            activity_pages = pages(sections, max(1, height - 1),
                                   quiet_seconds=layout["quiet_seconds"], max_sections=layout["max_sections"])
            index = selected_page(activity_pages, sections, session)
            rendered = render(_visible_output(directory, session, activity_pages[index]),
                              columns=min(width, 79) if session.get("canvas_width_warning") else width,
                              rows=height, page_index=index, page_count=len(activity_pages))
            if rendered != last or last_size != (width, height):
                # Absolute cursor moves never scroll a frame into tmux history.
                # Only these trusted positioning/clear sequences enter the pane.
                # Clear the whole current viewport, including rows left behind
                # by a prior longer frame, without addressing past its bottom.
                payload = "".join(f"\x1b[{row};1H\x1b[2K{rendered[row - 1] if row <= len(rendered) else ''}"
                                  for row in range(1, height + 1))
                os.write(1, payload.encode("utf-8", "replace"))
                last = rendered
                last_size = (width, height)
        except (OSError, KeyError, TypeError, ValueError, ShellCommandError):
            pass
        time.sleep(interval)


def run_native_summaries(session_record: Path) -> int:
    """Paint one row per collapsed job; this helper has no dispatch authority."""
    last: tuple[str, ...] | None = None
    last_size: tuple[int, int] | None = None
    while True:
        try:
            session = read_json(session_record, limit=2_000_000)
            if session.get("state") in {"closed", "removed"}:
                return 0
            if session.get("state") != "ready":
                time.sleep(.1)
                continue
            width, height = _terminal_size()
            sections = session.get("native_layout", {}).get("collapsed_sections", [])
            lines = tuple(_clip(safe_text(f"{item['operation_id']}  {item['state']}"),
                                max(0, width - 2))
                          for item in sections[:height])
            frame = lines + ("",) * max(0, height - len(lines))
            if frame != last or (width, height) != last_size:
                from .command_theme import ansi, palette
                colors = palette(session)
                style = ansi(colors["text"], colors["toolbar"])
                payload = "".join(f"\x1b[{row};1H{style}\x1b[2K {line}\x1b[K\x1b[0m"
                                  for row, line in enumerate(frame, 1))
                os.write(1, payload.encode("utf-8", "replace"))
                last = frame
                last_size = (width, height)
        except ShellCommandError:
            if not session_record.exists():
                return 0
        except (OSError, KeyError, TypeError, ValueError):
            pass
        time.sleep(.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-record", type=Path, required=True)
    parser.add_argument("--summaries", action="store_true")
    parser.add_argument("--ui-generation")
    parser.add_argument("--page-step", type=int)
    parser.add_argument("--client-pid", type=int)
    parser.add_argument("--mouse-x", type=int)
    parser.add_argument("--mouse-y", type=int)
    args = parser.parse_args()
    if not args.session_record.is_absolute() or args.session_record.name != "session.json":
        return 2
    if args.summaries:
        return run_native_summaries(args.session_record)
    if args.page_step is not None:
        return 0 if args.client_pid is not None and page_step(
            args.session_record, args.client_pid, args.page_step,
            mouse_x=args.mouse_x, mouse_y=args.mouse_y) else 2
    return run_renderer(args.session_record)


if __name__ == "__main__":
    raise SystemExit(main())

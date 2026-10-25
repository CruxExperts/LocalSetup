"""Pure, deterministic row planning for the native shell canvas."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence
from math import gcd


MODES = frozenset({"follow", "stable", "fixed"})
MIN_PANE_ROWS = 3
ANCHOR_ROWS = 1
DIVIDER_ROWS = 1
QUIET_SECONDS = 10.0
# Explicit user weights are limited to 10,000 by the command surface. Exact
# equal-share insertion can require larger internal integers (10000:1 becomes
# 20000:2:10001), so persisted derived weights have a separate finite bound.
MAX_DERIVED_WEIGHT = 1_000_000_000_000_000_000


def _sort_key(operation: Mapping[str, Any]) -> tuple[int, float, str]:
    activation = operation.get("layout_order", operation.get("activation_seq", 0))
    if type(activation) is not int or activation < 0:
        activation = 0
    created = operation.get("created_at", 0)
    if type(created) not in {int, float} or not math.isfinite(float(created)):
        created = 0.0
    return activation, float(created), str(operation.get("operation_id", ""))


def _recent(operation: Mapping[str, Any], now: float, quiet_seconds: float) -> bool:
    activity = operation.get("activity_monotonic", operation.get("activated_monotonic", now))
    if type(activity) not in {int, float} or not math.isfinite(float(activity)):
        activity = now
    lease = operation.get("input_required") is True
    state = operation.get("state")
    return lease or (state in {"running", "draining"}
                     and max(0.0, now - float(activity)) < quiet_seconds)


def _selected(operations: Sequence[Mapping[str, Any]], requested: object) -> str | None:
    ids = {str(operation["operation_id"]) for operation in operations
           if isinstance(operation.get("operation_id"), str)}
    if isinstance(requested, str) and requested in ids:
        return requested
    if not operations:
        return None
    latest = max(operations, key=lambda item: (_sort_key(item),
                                                float(item.get("activity_monotonic", 0)
                                                      if type(item.get("activity_monotonic", 0)) in {int, float}
                                                      else 0)))
    return str(latest["operation_id"])


def _activity_label(operation: Mapping[str, Any], now: float, quiet_seconds: float) -> str:
    if operation.get("input_required") is True:
        return "input needed"
    if operation.get("state") in {"running", "draining"}:
        return "active" if _recent(operation, now, quiet_seconds) else "idle"
    return str(operation.get("state", "unknown"))


def reactivation_order(order: Sequence[str], events: Sequence[tuple[float, str]]) -> list[str]:
    """Move every resumed job below older work, with stable simultaneous ties."""
    known = set(order)
    latest: dict[str, float] = {}
    for moment, operation_id in events:
        if operation_id in known:
            latest[operation_id] = max(moment, latest.get(operation_id, moment))
    resumed = sorted(latest, key=lambda operation_id: (latest[operation_id], operation_id))
    return [operation_id for operation_id in order if operation_id not in latest] + resumed


def allocate_rows(
    operations: Sequence[Mapping[str, Any]], weights: Mapping[str, int],
    *, rows: int, anchor_rows: int = ANCHOR_ROWS,
    min_pane_rows: int = MIN_PANE_ROWS,
    divider_rows: int = DIVIDER_ROWS,
) -> dict[str, int] | None:
    """Allocate every visible operation at least three rows using stable rounding."""
    count = len(operations)
    if count == 0:
        return {}
    if (type(rows) is not int or type(anchor_rows) is not int
            or type(min_pane_rows) is not int or type(divider_rows) is not int
            or rows < 1 or anchor_rows < 0 or min_pane_rows < 3 or divider_rows < 0):
        return None
    # The status window uses pane-border-status=top. Its control pane consumes
    # one border row even when its content is resized to one row. Each joined
    # task also needs a separator. These are physical tmux rows, not content.
    separators = (count + 1) * divider_rows
    available = rows - anchor_rows - separators
    minimums = [max(min_pane_rows,
                    item.get("native_min_rows", min_pane_rows)
                    if type(item.get("native_min_rows", min_pane_rows)) is int else min_pane_rows)
                for item in operations]
    minima = sum(minimums)
    if available < minima:
        return None
    extras = available - minima
    positive = [max(1, int(weights.get(str(item["operation_id"]), 1))) for item in operations]
    denominator = sum(positive)
    allocations = [minimum + (extras * weight // denominator)
                   for minimum, weight in zip(minimums, positive)]
    remaining = available - sum(allocations)
    remainders = [(extras * weight % denominator, index)
                  for index, weight in enumerate(positive)]
    for _remainder, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
        allocations[index] += 1
    return {str(item["operation_id"]): height
            for item, height in zip(operations, allocations)}


def plan_native_layout(
    operations: Sequence[Mapping[str, Any]], state: Mapping[str, Any],
    *, now: float, automatic_page: bool = False,
    quiet_seconds: float = QUIET_SECONDS,
    min_pane_rows: int = MIN_PANE_ROWS,
    anchor_rows: int = ANCHOR_ROWS,
    divider_rows: int = DIVIDER_ROWS,
    completed_hide_seconds: int = 60,
) -> dict[str, Any]:
    """Build a backend-independent page and row plan without reordering on output."""
    mode = state.get("mode", "follow")
    viewport = state.get("rows", 24)
    columns = state.get("columns", 120)
    if mode not in MODES:
        raise ValueError("unsupported native layout mode")
    if (type(viewport) is not int or viewport < 1
            or type(columns) is not int or not 80 <= columns <= 500
            or type(now) not in {int, float} or not math.isfinite(float(now))
            or type(quiet_seconds) not in {int, float} or quiet_seconds < 0):
        raise ValueError("invalid native canvas geometry or time")

    if type(completed_hide_seconds) is not int or not 1 <= completed_hide_seconds <= 86400:
        raise ValueError("invalid completed section timeout")
    pinned = set(state.get("expanded_operation_ids", []))
    inspected = state.get("inspection_target_id")
    def expired(item):
        completed = item.get("finished_monotonic")
        activity = item.get("activity_monotonic", completed)
        return (item.get("state") in {"completed", "failed"}
                and item.get("process_scope") == "collected"
                and item.get("output_drain") in {"available", "unavailable"}
                and item.get("operation_id") not in pinned
                and item.get("operation_id") != inspected
                and not item.get("input_required")
                and type(completed) in {int, float} and math.isfinite(completed)
                and type(activity) in {int, float} and math.isfinite(activity)
                and now - max(completed, activity) >= completed_hide_seconds)
    raw_operations = [item for item in operations
                      if isinstance(item.get("operation_id"), str)]
    raw_order = state.get("operation_order", [])
    order_rank = ({operation_id: index for index, operation_id in enumerate(raw_order)
                   if isinstance(operation_id, str)} if isinstance(raw_order, list) else {})
    inventory_order = sorted(raw_operations, key=lambda item: (
        order_rank.get(str(item["operation_id"]), len(order_rank) + _sort_key(item)[0]),
        _sort_key(item)))
    # Expiry removes rendered sections, not native inventory. Keep parked-pane
    # assignments so the backend can still verify every owned operation pair.
    ordered = [item for item in inventory_order if not expired(item)]
    selected = _selected(ordered, state.get("selected_operation_id"))
    if mode == "follow":
        expanded_ids = set(state.get("expanded_operation_ids", []))
        candidates = [item for item in ordered
                      if (_recent(item, float(now), float(quiet_seconds))
                          or str(item["operation_id"]) in expanded_ids)]
        if selected is not None:
            selected_item = next((item for item in ordered
                                  if item["operation_id"] == selected), None)
            if selected_item is not None and selected_item not in candidates:
                candidates.append(selected_item)
                candidates.sort(key=lambda item: (
                    order_rank.get(str(item["operation_id"]), len(order_rank) + _sort_key(item)[0]),
                    _sort_key(item)))
    else:
        visible_ids = state.get("visible_operation_ids", [])
        visible = set(visible_ids) if isinstance(visible_ids, list) else set()
        candidates = [item for item in ordered if item["operation_id"] in visible]
        if selected is not None:
            selected_item = next((item for item in ordered
                                  if item["operation_id"] == selected), None)
            if selected_item is not None and selected_item not in candidates:
                candidates.append(selected_item)
                candidates.sort(key=lambda item: (
                    order_rank.get(str(item["operation_id"]), len(order_rank) + _sort_key(item)[0]),
                    _sort_key(item)))

    grouped = state.get("grouped_sections") is True
    # Dormant workloads keep their native PTY parked, but occupy one visible
    # summary row. A single shared strip owns their rows and one header border.
    # Older live sessions without this capability retain their existing layout.
    compact = state.get("compact_idle") is True
    active_ids = {str(item["operation_id"]) for item in candidates}
    collapsed = [item for item in ordered if item["operation_id"] not in active_ids] if compact else []
    entries = ordered if grouped else collapsed + candidates
    maximum = state.get("max_sections", 8)
    if type(maximum) is not int or not 1 <= maximum <= 4096:
        raise ValueError("invalid maximum sections")
    if grouped:
        maximum = min(maximum, 8)  # The fresh canvas owns eight qualified summary slots.
    page_budget = viewport - anchor_rows - divider_rows
    pages: list[list[Mapping[str, Any]]] = [[]]
    used_rows = 0
    unplaceable = False
    has_summaries = False
    for operation in entries:
        requested_minimum = operation.get("native_min_rows", min_pane_rows)
        minimum = max(min_pane_rows, requested_minimum
                      if type(requested_minimum) is int else min_pane_rows)
        is_summary = operation["operation_id"] not in active_ids
        cost = (1 + (0 if has_summaries else divider_rows)) if is_summary else minimum + divider_rows
        if pages[-1] and (used_rows + cost > page_budget or len(pages[-1]) >= maximum):
            pages.append([])
            used_rows = 0
            has_summaries = False
            cost = (1 + divider_rows) if is_summary else minimum + divider_rows
        if cost > page_budget:
            unplaceable = True
            break
        pages[-1].append(operation)
        used_rows += cost
        has_summaries = is_summary if grouped else has_summaries or is_summary
    if unplaceable or (pages == [[]] and candidates and page_budget < min_pane_rows):
        return {
            "version": 1, "generation": int(state.get("desired_generation", 0)),
            "mode": mode, "viewport": {"columns": columns, "rows": viewport},
            "anchor_rows": anchor_rows, "divider_rows": divider_rows,
            "selected_operation_id": selected, "page": 0,
            "page_count": 1, "blocked": "viewport_too_short",
            "candidate_operation_ids": [str(item["operation_id"]) for item in candidates],
            "assignments": [{"operation_id": str(item["operation_id"]),
                             "rows": min_pane_rows, "order": index, "visible": False}
                            for index, item in enumerate(inventory_order)],
        }

    if not entries:
        pages = [[]]
    page_count = len(pages)
    if (mode == "follow" and not state.get("manual_page")) or automatic_page:
        selected_page = next((index for index, page in enumerate(pages)
                              if any(item["operation_id"] == selected for item in page)), 0)
    else:
        requested_page = state.get("page", 0)
        if type(requested_page) is not int:
            requested_page = 0
        selected_page = min(max(0, requested_page), page_count - 1)
        anchor = state.get("page_anchor_operation_id")
        if anchor is not None and not any(item["operation_id"] == anchor for item in pages[selected_page]):
            selected_page = next((index for index, page in enumerate(pages)
                                  if any(item["operation_id"] == anchor for item in page)), selected_page)
    all_page = pages[selected_page]
    page = [item for item in all_page if item["operation_id"] in active_ids]
    summaries = [item for item in all_page if item["operation_id"] not in active_ids]
    if not any(item["operation_id"] == selected for item in page):
        selected = str(page[0]["operation_id"]) if page else None
    weights = state.get("weights", {})
    if not isinstance(weights, Mapping):
        weights = {}
    groups: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    for item in all_page:
        operation_id = str(item["operation_id"])
        if operation_id in active_ids:
            sections.append({"kind": "operation", "operation_id": operation_id})
        elif sections and sections[-1]["kind"] == "summary":
            sections[-1]["operation_ids"].append(operation_id)
        else:
            sections.append({"kind": "summary", "summary_index": len(groups),
                             "operation_ids": [operation_id]})
            groups.append(sections[-1])
    summary_cost = len(summaries) + (len(groups) * divider_rows if grouped else
                                     divider_rows if summaries else 0)
    allocation_weights = weights if mode == "fixed" or not grouped else {}
    rows_by_id = allocate_rows(page, allocation_weights, rows=viewport - summary_cost,
                               anchor_rows=anchor_rows,
                               min_pane_rows=min_pane_rows,
                               divider_rows=divider_rows)
    inspection_target = state.get("inspection_target_id") if grouped else None
    preserved = state.get("inspection_preserved_rows", {})
    if (rows_by_id is not None and isinstance(inspection_target, str)
            and isinstance(preserved, Mapping) and inspection_target in rows_by_id):
        retained = {str(item["operation_id"]): preserved.get(str(item["operation_id"]))
                    for item in page if item["operation_id"] != inspection_target}
        if all(type(value) is int and value >= min_pane_rows for value in retained.values()):
            available = viewport - summary_cost - anchor_rows - (len(page) + 1) * divider_rows
            target_rows = available - sum(retained.values())
            if target_rows >= min_pane_rows:
                rows_by_id = retained | {inspection_target: target_rows}
            else:
                # The lower pane stays a one-row summary when expanding it
                # would steal a row from the established panes above.
                fallback = dict(state)
                fallback["visible_operation_ids"] = [item for item in state.get("visible_operation_ids", [])
                                                      if item != inspection_target]
                fallback["selected_operation_id"] = next(iter(retained), None)
                fallback["inspection_target_id"] = None
                fallback["inspection_preserved_rows"] = None
                fallback["page_anchor_operation_id"] = None
                return plan_native_layout(operations, fallback, now=now,
                                          completed_hide_seconds=completed_hide_seconds,
                                          automatic_page=automatic_page,
                                          quiet_seconds=quiet_seconds,
                                          min_pane_rows=min_pane_rows,
                                          anchor_rows=anchor_rows,
                                          divider_rows=divider_rows)
    if rows_by_id is None:
        return {
            "version": 1, "generation": int(state.get("desired_generation", 0)),
            "mode": mode, "viewport": {"columns": columns, "rows": viewport},
            "anchor_rows": anchor_rows, "divider_rows": divider_rows,
            "selected_operation_id": selected, "page": selected_page,
            "page_count": page_count, "blocked": "viewport_too_short",
            "candidate_operation_ids": [str(item["operation_id"]) for item in candidates],
            "assignments": [{"operation_id": str(item["operation_id"]),
                             "rows": min_pane_rows, "order": index,
                             "visible": False} for index, item in enumerate(inventory_order)],
        }
    visible_ids = set(rows_by_id)
    for section in sections:
        if section["kind"] == "summary":
            section["rows"] = len(section["operation_ids"])
        else:
            section["rows"] = rows_by_id[section["operation_id"]]
    assignments: list[dict[str, Any]] = []
    for index, operation in enumerate(inventory_order):
        op_id = str(operation["operation_id"])
        old_native = operation.get("native_canvas")
        old_rows = old_native.get("pane_rows") if isinstance(old_native, Mapping) else None
        fallback = old_rows if type(old_rows) is int and old_rows >= min_pane_rows else min_pane_rows
        assignments.append({
            "operation_id": op_id,
            "rows": rows_by_id.get(op_id, fallback),
            "order": index,
            "visible": op_id in visible_ids,
        })
    return {
        "version": 1, "generation": int(state.get("desired_generation", 0)),
        "mode": mode, "viewport": {"columns": columns, "rows": viewport},
        "anchor_rows": anchor_rows, "divider_rows": divider_rows,
        "selected_operation_id": selected, "page": selected_page,
        "page_count": page_count, "blocked": None,
        "candidate_operation_ids": [str(item["operation_id"]) for item in candidates],
        "page_operation_ids": [str(item["operation_id"]) for item in all_page],
        "sections": sections if grouped else [],
        "collapsed_groups": groups if grouped else [],
        "collapsed_sections": [{"operation_id": str(item["operation_id"]),
                                "state": _activity_label(item, float(now), float(quiet_seconds)),
                                **({"pane_label": str(item.get("pane_label", item["operation_id"]))}
                                   if grouped else {})}
                               for item in summaries],
        "assignments": assignments,
    }


def default_weights_for_new_operation(
    weights: Mapping[str, int], existing_ids: Sequence[str], operation_id: str,
) -> dict[str, int]:
    """Preserve existing relative ratios and give a fixed-layout newcomer an equal share."""
    updated = {str(key): int(value) for key, value in weights.items()
               if isinstance(key, str) and type(value) is int and 1 <= value <= MAX_DERIVED_WEIGHT}
    if operation_id in updated:
        return updated
    prior_ids = [item for item in existing_ids if item in updated]
    prior = [updated[item] for item in prior_ids]
    if prior:
        count = len(prior)
        scaled = [value * count for value in prior]
        newcomer = sum(prior)
        divisor = 0
        for value in (*scaled, newcomer):
            divisor = gcd(divisor, value)
        divisor = max(1, divisor)
        scaled = [value // divisor for value in scaled]
        newcomer //= divisor
        if max((*scaled, newcomer)) > MAX_DERIVED_WEIGHT:
            raise ValueError("new fixed pane would exceed exact ratio precision")
        for key, value in zip(prior_ids, scaled):
            updated[key] = value
        updated[operation_id] = max(1, newcomer)
    else:
        updated[operation_id] = 1
    return updated

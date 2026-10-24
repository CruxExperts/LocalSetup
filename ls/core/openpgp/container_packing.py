"""Deterministic whole-file-first planning for serialized container volumes."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterator, Sequence


class PackingError(ValueError):
    """A requested packing plan cannot be represented within its bounds."""


class PackingLimitError(PackingError):
    """A configurable planner resource limit was reached."""


@dataclass(frozen=True)
class PackingItem:
    """A logical input file represented by its opaque stable planning key."""

    identity: str
    size_bytes: int


@dataclass(frozen=True)
class PackedPart:
    """A whole file or one contiguous segment passed to the encrypting caller."""

    identity: str
    offset: int
    length: int
    segment_index: int = 0
    segment_count: int = 1
    segmented: bool = False


@dataclass(frozen=True)
class PackingVolume:
    index: int
    parts: tuple[PackedPart, ...]
    serialized_bytes: int


@dataclass(frozen=True)
class PackingPlan:
    volumes: tuple[PackingVolume, ...]
    target_bytes: int
    tolerance: float
    effective_ceiling_bytes: int
    measure_calls: int


@dataclass(frozen=True)
class PackingLimits:
    target_bytes: int = 100_000_000
    tolerance: float = 0.10
    hard_ceiling_bytes: int | None = None
    max_entries: int = 100_000
    max_parts: int = 1_000_000
    max_volumes: int = 100_000
    max_split_iterations: int = 64
    max_measure_calls: int = 1_000_000
    max_total_plaintext_bytes: int = 1 << 50

    def __post_init__(self) -> None:
        for name in (
            "target_bytes",
            "max_entries",
            "max_parts",
            "max_volumes",
            "max_split_iterations",
            "max_measure_calls",
            "max_total_plaintext_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.hard_ceiling_bytes is not None and (
            isinstance(self.hard_ceiling_bytes, bool)
            or not isinstance(self.hard_ceiling_bytes, int)
            or self.hard_ceiling_bytes <= 0
        ):
            raise ValueError("hard_ceiling_bytes must be a positive integer or None")
        if isinstance(self.tolerance, bool) or not isinstance(self.tolerance, (int, float)):
            raise ValueError("tolerance must be a finite number between zero and one")
        if not math.isfinite(self.tolerance) or self.tolerance < 0 or self.tolerance > 1:
            raise ValueError("tolerance must be a finite number between zero and one")


MeasureSerializedSize = Callable[[tuple[PackedPart, ...]], int]


def plan_volumes(
    items: Sequence[PackingItem],
    *,
    measure_serialized_size: MeasureSerializedSize,
    limits: PackingLimits = PackingLimits(),
) -> PackingPlan:
    """Plan whole-file-first volumes using exact final serialized-size measures.

    The callback must deterministically return the exact byte size of the
    final encrypted and serialized candidate volume, including metadata,
    envelope, and YAML/base64 overhead. A facade may use cached ciphertext or
    deterministic measurement before sealing; after sealing, it must recheck
    the actual final size against the configured ceiling. This planner never
    estimates size from plaintext length and never pads a volume.
    """
    if not callable(measure_serialized_size):
        raise TypeError("measure_serialized_size must be callable")
    if isinstance(items, (str, bytes, bytearray)):
        raise TypeError("items must be a sequence of PackingItem values")
    if len(items) > limits.max_entries:
        raise PackingLimitError("entry count exceeds the configured limit")
    seen: set[str] = set()
    total_plaintext = 0
    for item in items:
        if not isinstance(item, PackingItem):
            raise TypeError("items must contain PackingItem values")
        if not item.identity or item.identity in seen:
            raise PackingError("item identities must be non-empty and unique")
        seen.add(item.identity)
        if isinstance(item.size_bytes, bool) or not isinstance(item.size_bytes, int) or item.size_bytes < 0:
            raise PackingError("item size must be a non-negative integer")
        total_plaintext += item.size_bytes
        if total_plaintext > limits.max_total_plaintext_bytes:
            raise PackingLimitError("total plaintext size exceeds the configured limit")

    target_with_tolerance = _soft_ceiling(limits)
    effective_ceiling = min(
        target_with_tolerance,
        limits.hard_ceiling_bytes if limits.hard_ceiling_bytes is not None else target_with_tolerance,
    )
    measurer = _Measured(measure_serialized_size, limits)
    completed: list[PackingVolume] = []
    current: list[PackedPart] = []
    current_size = 0
    part_count = 0

    def close_current() -> None:
        nonlocal current, current_size
        if not current:
            return
        if len(completed) >= limits.max_volumes:
            raise PackingLimitError("volume count exceeds the configured limit")
        completed.append(
            PackingVolume(index=len(completed), parts=tuple(current), serialized_bytes=current_size)
        )
        current = []
        current_size = 0

    def append_measured(part: PackedPart) -> None:
        nonlocal current, current_size, part_count
        if part_count >= limits.max_parts:
            raise PackingLimitError("planned part count exceeds the configured limit")
        if current:
            candidate = tuple((*current, part))
            candidate_size = measurer(candidate)
            if candidate_size <= effective_ceiling:
                current.append(part)
                current_size = candidate_size
                part_count += 1
                return
            close_current()
        fresh_size = measurer((part,))
        if fresh_size > effective_ceiling:
            raise PackingError("planned part does not fit in a fresh volume")
        current = [part]
        current_size = fresh_size
        part_count += 1

    for item in items:
        whole = PackedPart(identity=item.identity, offset=0, length=item.size_bytes)
        if current:
            candidate_size = measurer(tuple((*current, whole)))
            if candidate_size <= effective_ceiling:
                if part_count >= limits.max_parts:
                    raise PackingLimitError("planned part count exceeds the configured limit")
                current.append(whole)
                current_size = candidate_size
                part_count += 1
                continue
            close_current()

        whole_size = measurer((whole,))
        if whole_size <= effective_ceiling:
            if part_count >= limits.max_parts:
                raise PackingLimitError("planned part count exceeds the configured limit")
            current = [whole]
            current_size = whole_size
            part_count += 1
            continue

        segment_size, segments = _split_oversized_item(
            item,
            measure=measurer,
            ceiling=effective_ceiling,
            limits=limits,
            max_parts_remaining=limits.max_parts - part_count,
        )
        for segment in segments:
            append_measured(segment)

    close_current()
    return PackingPlan(
        volumes=tuple(completed),
        target_bytes=limits.target_bytes,
        tolerance=limits.tolerance,
        effective_ceiling_bytes=effective_ceiling,
        measure_calls=measurer.calls,
    )


class _Measured:
    def __init__(self, callback: MeasureSerializedSize, limits: PackingLimits) -> None:
        self.callback = callback
        self.limits = limits
        self.cache: OrderedDict[PackedPart, int] = OrderedDict()
        self.calls = 0

    def __call__(self, parts: tuple[PackedPart, ...]) -> int:
        cache_key = parts[0] if len(parts) == 1 else None
        if cache_key is not None and cache_key in self.cache:
            self.cache.move_to_end(cache_key)
            return self.cache[cache_key]
        if self.calls >= self.limits.max_measure_calls:
            raise PackingLimitError("serialized-size measurement count exceeds the configured limit")
        measured = self.callback(parts)
        self.calls += 1
        if isinstance(measured, bool) or not isinstance(measured, int) or measured <= 0:
            raise PackingError("size callback must return a positive serialized byte count")
        if cache_key is not None:
            self.cache[cache_key] = measured
            self.cache.move_to_end(cache_key)
            if len(self.cache) > 4096:
                self.cache.popitem(last=False)
        return measured


def _split_oversized_item(
    item: PackingItem,
    *,
    measure: _Measured,
    ceiling: int,
    limits: PackingLimits,
    max_parts_remaining: int,
) -> tuple[int, Iterator[PackedPart]]:
    if item.size_bytes < 2:
        raise PackingError("an item exceeds a fresh-volume ceiling and cannot be segmented")
    if max_parts_remaining <= 0:
        raise PackingLimitError("planned part count exceeds the configured limit")
    low = 1
    high = item.size_bytes - 1
    best: tuple[int, int] | None = None
    iterations = 0
    while low <= high and iterations < limits.max_split_iterations:
        iterations += 1
        candidate_size = (low + high) // 2
        candidate_count = (item.size_bytes + candidate_size - 1) // candidate_size
        if candidate_count > max_parts_remaining:
            low = candidate_size + 1
            continue
        fits = all(
            measure((segment,)) <= ceiling
            for segment in _iter_segments_for(item, candidate_size, candidate_count)
        )
        if fits:
            best = (candidate_size, candidate_count)
            low = candidate_size + 1
        else:
            high = candidate_size - 1
    if best is None:
        raise PackingLimitError("no safe segment size found within the configured resource limits")
    segment_size, segment_count = best
    return segment_size, _iter_segments_for(item, segment_size, segment_count)


def _iter_segments_for(
    item: PackingItem,
    segment_size: int,
    segment_count: int,
) -> Iterator[PackedPart]:
    for index, offset in enumerate(range(0, item.size_bytes, segment_size)):
        yield PackedPart(
            identity=item.identity,
            offset=offset,
            length=min(segment_size, item.size_bytes - offset),
            segment_index=index,
            segment_count=segment_count,
            segmented=True,
        )


def _soft_ceiling(limits: PackingLimits) -> int:
    try:
        tolerance = Decimal(str(limits.tolerance))
        return limits.target_bytes + int(Decimal(limits.target_bytes) * tolerance)
    except (InvalidOperation, OverflowError) as exc:
        raise PackingError("target tolerance cannot be represented") from exc

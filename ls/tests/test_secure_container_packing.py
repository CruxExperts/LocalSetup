from __future__ import annotations

import math

import pytest

from ls.core.openpgp.container_packing import (
    PackedPart,
    PackingError,
    PackingItem,
    PackingLimitError,
    PackingLimits,
    plan_volumes,
)


def _binary_yaml_size(length: int, *, indent: int) -> int:
    encoded = 4 * ((length + 2) // 3)
    lines = math.ceil(encoded / 76)
    return indent * lines + encoded + lines


def _exact_final_size(parts: tuple[PackedPart, ...]) -> int:
    """Model final encryption, encrypted inventory, and canonical YAML bytes."""
    metadata_ciphertext = 20 + 8 * len(parts)
    header_and_yaml_keys = 100 + _binary_yaml_size(metadata_ciphertext, indent=2)
    encrypted_messages = sum(
        len(b"  - !!binary |\n")
        + _binary_yaml_size(part.length + 12, indent=4)
        for part in parts
    )
    return header_and_yaml_keys + encrypted_messages


def test_default_target_is_soft_and_whole_files_start_new_volumes_before_splitting() -> None:
    calls: list[tuple[PackedPart, ...]] = []

    def measure(parts: tuple[PackedPart, ...]) -> int:
        calls.append(parts)
        return _exact_final_size(parts)

    whole_slightly_over_nominal = plan_volumes(
        [PackingItem("one", 600)],
        measure_serialized_size=measure,
        limits=PackingLimits(target_bytes=1000, tolerance=0.10),
    )
    assert whole_slightly_over_nominal.volumes[0].serialized_bytes > 1000
    assert whole_slightly_over_nominal.volumes[0].serialized_bytes <= 1100
    assert not whole_slightly_over_nominal.volumes[0].parts[0].segmented

    plan = plan_volumes(
        [PackingItem("first", 1200), PackingItem("second", 400)],
        measure_serialized_size=measure,
        limits=PackingLimits(target_bytes=2000, tolerance=0.10),
    )
    assert len(plan.volumes) == 2
    assert [volume.parts[0].identity for volume in plan.volumes] == ["first", "second"]
    assert all(len(volume.parts) == 1 and not volume.parts[0].segmented for volume in plan.volumes)
    assert all(volume.serialized_bytes == measure(volume.parts) for volume in plan.volumes)
    assert plan.measure_calls <= len(calls)


def test_oversized_file_segments_use_exact_final_sizes_and_hard_ceiling() -> None:
    limits = PackingLimits(target_bytes=2000, tolerance=0.10, hard_ceiling_bytes=1200)
    plan = plan_volumes(
        [PackingItem("large", 2500)],
        measure_serialized_size=_exact_final_size,
        limits=limits,
    )
    parts = [part for volume in plan.volumes for part in volume.parts]
    assert len(parts) > 1
    assert all(part.segmented for part in parts)
    assert all(part.segment_count == len(parts) for part in parts)
    assert [part.segment_index for part in parts] == list(range(len(parts)))
    assert [part.offset for part in parts][0] == 0
    assert all(left.offset + left.length == right.offset for left, right in zip(parts, parts[1:]))
    assert sum(part.length for part in parts) == 2500
    assert all(volume.serialized_bytes <= 1200 for volume in plan.volumes)
    assert all(volume.serialized_bytes == _exact_final_size(volume.parts) for volume in plan.volumes)
    assert plan.effective_ceiling_bytes == 1200


def test_candidate_volume_measurement_includes_metadata_and_serialization_overhead() -> None:
    items = (PackingItem("a", 600), PackingItem("b", 150))
    observed: list[tuple[PackedPart, ...]] = []

    def measure(parts: tuple[PackedPart, ...]) -> int:
        observed.append(parts)
        return _exact_final_size(parts)

    plan = plan_volumes(
        items,
        measure_serialized_size=measure,
        limits=PackingLimits(target_bytes=1100, tolerance=0.10, hard_ceiling_bytes=1300),
    )
    assert any(len(candidate) == 2 for candidate in observed)
    assert all(volume.serialized_bytes <= 1300 for volume in plan.volumes)
    assert all(volume.serialized_bytes == measure(volume.parts) for volume in plan.volumes)


def test_planner_has_explicit_entry_measurement_and_impossible_segment_limits() -> None:
    with pytest.raises(PackingLimitError):
        plan_volumes(
            [PackingItem("a", 1), PackingItem("b", 1)],
            measure_serialized_size=lambda parts: 1,
            limits=PackingLimits(max_entries=1),
        )
    with pytest.raises(PackingError):
        plan_volumes(
            [PackingItem("a", 1)],
            measure_serialized_size=lambda parts: 100,
            limits=PackingLimits(target_bytes=10, tolerance=0, hard_ceiling_bytes=20),
        )
    with pytest.raises(ValueError):
        plan_volumes(
            [PackingItem("a", 20)],
            measure_serialized_size=lambda parts: 10,
            limits=PackingLimits(tolerance=float("nan")),
        )


def test_part_limit_applies_when_a_segment_is_added_to_a_nonempty_volume() -> None:
    def measure(parts: tuple[PackedPart, ...]) -> int:
        return 10 + max(part.length for part in parts)

    with pytest.raises(PackingLimitError):
        plan_volumes(
            [PackingItem("oversized", 20)],
            measure_serialized_size=measure,
            limits=PackingLimits(target_bytes=15, tolerance=0, max_parts=1),
        )


def test_segment_count_limit_bounds_measurement_for_a_large_input() -> None:
    calls = 0

    def measure(parts: tuple[PackedPart, ...]) -> int:
        nonlocal calls
        calls += 1
        return 10 + max(part.length for part in parts)

    with pytest.raises(PackingLimitError):
        plan_volumes(
            [PackingItem("large", 1_000_000)],
            measure_serialized_size=measure,
            limits=PackingLimits(
                target_bytes=20,
                tolerance=0,
                max_parts=3,
                max_measure_calls=64,
            ),
        )
    assert calls < 64

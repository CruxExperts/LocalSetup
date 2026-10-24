from __future__ import annotations

import itertools

import pytest

from ls.core.openpgp.container_versions import (
    VersionNode, build_version_graph, common_ancestors, merge_requirements,
    version_dependencies, version_heads,
)


ARTIFACT = "1" * 32
ROOT = "2" * 32
LEFT = "3" * 32
RIGHT = "4" * 32
MERGED = "5" * 32
MISSING = "6" * 32


def _node(version: str, parents: tuple[str, ...], commitment: str) -> VersionNode:
    return VersionNode(ARTIFACT, version, parents, commitment)


def test_version_graph_is_order_independent_and_preserves_divergent_heads() -> None:
    nodes = (
        _node(ROOT, (), "a" * 64),
        _node(LEFT, (ROOT,), "b" * 64),
        _node(RIGHT, (ROOT,), "c" * 64),
    )
    projections = [build_version_graph(order) for order in itertools.permutations(nodes)]

    assert all(graph == projections[0] for graph in projections)
    assert version_heads(projections[0], artifact_id=ARTIFACT) == (LEFT, RIGHT)
    assert common_ancestors(projections[0], ARTIFACT, (LEFT, RIGHT)) == (ROOT,)
    requirement = merge_requirements(projections[0], artifact_id=ARTIFACT)
    assert requirement is not None
    assert requirement.required_parents == (LEFT, RIGHT)
    assert requirement.common_ancestors == (ROOT,)
    assert requirement.requires_explicit_authorization is True


def test_explicit_merge_is_a_new_node_with_both_immutable_parents() -> None:
    graph = build_version_graph((
        _node(ROOT, (), "a" * 64),
        _node(LEFT, (ROOT,), "b" * 64),
        _node(RIGHT, (ROOT,), "c" * 64),
        _node(MERGED, tuple(sorted((LEFT, RIGHT))), "d" * 64),
    ))

    assert version_heads(graph, artifact_id=ARTIFACT) == (MERGED,)
    assert common_ancestors(graph, ARTIFACT, (MERGED, LEFT, RIGHT)) == (ROOT,)
    assert graph.nodes[0].version_id == ROOT


def test_missing_parent_is_explicit_and_never_filled_by_a_winner() -> None:
    graph = build_version_graph((_node(LEFT, (MISSING,), "b" * 64),))

    assert graph.missing_parents == ((ARTIFACT, MISSING),)
    assert version_heads(graph, artifact_id=ARTIFACT) == (LEFT,)
    assert version_dependencies(graph)["missing"] == ((ARTIFACT, MISSING),)
    requirement = merge_requirements(graph, artifact_id=ARTIFACT)
    assert requirement is None


def test_version_identity_collision_and_cycle_are_reported_without_selection() -> None:
    collision = build_version_graph((
        _node(LEFT, (), "b" * 64),
        _node(LEFT, (), "c" * 64),
    ))
    assert collision.collisions == ((ARTIFACT, LEFT),)
    assert version_heads(collision, artifact_id=ARTIFACT) == ()
    requirement = merge_requirements(collision, artifact_id=ARTIFACT)
    assert requirement is not None
    assert requirement.heads == ()
    assert requirement.unresolved_dependencies == ()

    cycle = build_version_graph((
        _node(LEFT, (RIGHT,), "b" * 64),
        _node(RIGHT, (LEFT,), "c" * 64),
    ))
    assert cycle.cycles
    assert version_heads(cycle, artifact_id=ARTIFACT) == ()


def test_unrelated_artifact_collision_does_not_hide_other_heads() -> None:
    other = "7" * 32
    graph = build_version_graph((
        _node(LEFT, (), "b" * 64),
        VersionNode(other, LEFT, (), "c" * 64),
        VersionNode(other, LEFT, (), "d" * 64),
    ))

    assert graph.collisions == ((other, LEFT),)
    assert version_heads(graph, artifact_id=ARTIFACT) == (LEFT,)


def test_graph_work_is_bounded() -> None:
    from ls.core.openpgp.container_versions import VersionGraphError, VersionGraphErrorCode

    nodes = (_node(f"{index:032x}", (), f"{index:064x}") for index in range(1, 10_002))
    with pytest.raises(VersionGraphError) as raised:
        build_version_graph(nodes)
    assert raised.value.code is VersionGraphErrorCode.BOUNDED


def test_large_cycle_uses_bounded_canonicalization() -> None:
    total = 10_000
    nodes = (
        _node(f"{index:032x}", (f"{(index % total) + 1:032x}",),
              f"{index:064x}")
        for index in range(1, total + 1)
    )
    graph = build_version_graph(nodes)
    assert len(graph.cycles) == 1
    assert len(graph.cycles[0]) == total
    assert graph.cycles[0][0] == (ARTIFACT, f"{1:032x}")


def test_nine_divergent_heads_preserve_all_merge_parents() -> None:
    children = tuple(f"{index:032x}" for index in range(10, 19))
    graph = build_version_graph((
        _node(ROOT, (), "a" * 64),
        *(_node(version, (ROOT,), f"{index:064x}")
          for index, version in enumerate(children, start=10)),
    ))
    requirement = merge_requirements(graph, artifact_id=ARTIFACT)
    assert requirement is not None
    assert requirement.heads == children
    assert requirement.common_ancestors == (ROOT,)
    assert requirement.required_parents == children


def test_common_ancestor_input_generator_is_bounded_before_materialization() -> None:
    from ls.core.openpgp.container_versions import VersionGraphError, VersionGraphErrorCode
    graph = build_version_graph((_node(ROOT, (), "a" * 64),))
    consumed = 0
    def unbounded_versions():
        nonlocal consumed
        while True:
            consumed += 1
            yield f"{consumed:032x}"
    with pytest.raises(VersionGraphError) as raised:
        common_ancestors(graph, ARTIFACT, unbounded_versions())
    assert raised.value.code is VersionGraphErrorCode.BOUNDED
    assert consumed <= 10_001


def test_many_long_backedges_stop_before_cycle_tuple_expansion() -> None:
    from ls.core.openpgp.container_versions import VersionGraphError, VersionGraphErrorCode

    total = 600
    root = f"{1:032x}"
    nodes = [_node(root, (f"{2:032x}",), "a" * 64)]
    for index in range(2, total + 1):
        parents = [root]
        if index < total:
            parents.append(f"{index + 1:032x}")
        nodes.append(_node(f"{index:032x}", tuple(sorted(parents)),
                           f"{index:064x}"))
    with pytest.raises(VersionGraphError) as raised:
        build_version_graph(nodes)
    assert raised.value.code is VersionGraphErrorCode.BOUNDED

"""Deterministic immutable version-graph projections for secure containers.

This module never resolves conflicts or creates a merged payload. It describes
the graph already authenticated in protected metadata and reports the work a
separately authorized merge would require.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .artifact_records import validate_digest, validate_opaque_id


class VersionGraphErrorCode(StrEnum):
    INVALID = "INVALID"
    COLLISION = "COLLISION"
    CYCLE = "CYCLE"
    BOUNDED = "BOUNDED"


class VersionGraphError(ValueError):
    """A malformed or conflicting immutable version graph."""

    def __init__(self, code: VersionGraphErrorCode) -> None:
        self.code = VersionGraphErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class VersionNode:
    """One immutable version claim; timestamps never determine ordering."""

    artifact_id: str
    version_id: str
    parents: tuple[str, ...]
    commitment_sha256: str

    def __post_init__(self) -> None:
        try:
            validate_opaque_id(self.artifact_id)
            validate_opaque_id(self.version_id)
            if (
                type(self.parents) is not tuple
                or len(self.parents) > 64
                or any(type(item) is not str for item in self.parents)
            ):
                raise ValueError
            for parent in self.parents:
                validate_opaque_id(parent)
            if self.parents != tuple(sorted(set(self.parents))):
                raise ValueError
            validate_digest(self.commitment_sha256)
        except Exception:
            raise VersionGraphError(VersionGraphErrorCode.INVALID) from None

    @property
    def key(self) -> tuple[str, str]:
        return self.artifact_id, self.version_id


@dataclass(frozen=True, slots=True)
class VersionGraph:
    """Canonical graph input with explicit collisions and missing parents."""

    nodes: tuple[VersionNode, ...]
    collisions: tuple[tuple[str, str], ...]
    missing_parents: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True, slots=True)
class MergeRequirement:
    artifact_id: str
    heads: tuple[str, ...]
    common_ancestors: tuple[str, ...]
    unresolved_dependencies: tuple[str, ...]
    required_parents: tuple[str, ...]
    requires_explicit_authorization: bool = True


_MAX_NODES = 10_000
_MAX_EDGES = 100_000


def build_version_graph(nodes: Iterable[VersionNode]) -> VersionGraph:
    """Normalize an authenticated node set without choosing collision winners."""
    if isinstance(nodes, (str, bytes, bytearray, dict)):
        raise VersionGraphError(VersionGraphErrorCode.INVALID)
    grouped: dict[tuple[str, str], set[VersionNode]] = {}
    node_count = 0
    edge_count = 0
    try:
        for node in nodes:
            node_count += 1
            edge_count += len(node.parents) if isinstance(node, VersionNode) else 0
            if node_count > _MAX_NODES or edge_count > _MAX_EDGES:
                raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
            if not isinstance(node, VersionNode):
                raise VersionGraphError(VersionGraphErrorCode.INVALID)
            grouped.setdefault(node.key, set()).add(node)
    except VersionGraphError:
        raise
    except Exception:
        raise VersionGraphError(VersionGraphErrorCode.INVALID) from None

    collisions = tuple(sorted(key for key, values in grouped.items() if len(values) > 1))
    selected = tuple(
        min(values, key=lambda item: (item.commitment_sha256, item.parents))
        for key, values in sorted(grouped.items())
        if len(values) == 1
    )
    keys = {node.key for node in selected}
    missing = tuple(sorted(
        (node.artifact_id, parent)
        for node in selected for parent in node.parents
        if (node.artifact_id, parent) not in keys
    ))
    cycles = _cycles(selected, set(collisions))
    return VersionGraph(selected, collisions, missing, cycles)


def version_dependencies(graph: VersionGraph | Iterable[VersionNode]) -> dict[str, object]:
    """Return sorted known and unresolved parent edges for each immutable node."""
    selected = _graph(graph)
    known = {node.key for node in selected.nodes}
    return {
        "dependencies": tuple(
            (node.artifact_id, node.version_id, tuple(node.parents))
            for node in selected.nodes
        ),
        "missing": selected.missing_parents,
        "collisions": selected.collisions,
        "cycles": selected.cycles,
        "known": tuple(sorted(known)),
    }


def version_heads(
    graph: VersionGraph | Iterable[VersionNode], *, artifact_id: str | None = None,
) -> tuple[str, ...]:
    """Return all maximal known versions; no timestamp or arrival winner exists."""
    selected = _graph(graph)
    if artifact_id is not None:
        try:
            validate_opaque_id(artifact_id)
        except Exception:
            raise VersionGraphError(VersionGraphErrorCode.INVALID) from None
    if _has_graph_issue(selected, artifact_id):
        return ()
    nodes = [node for node in selected.nodes
             if artifact_id is None or node.artifact_id == artifact_id]
    parents = {
        (node.artifact_id, parent)
        for node in nodes for parent in node.parents
    }
    return tuple(sorted(
        node.version_id for node in nodes
        if (node.artifact_id, node.version_id) not in parents
    ))


def common_ancestors(
    graph: VersionGraph | Iterable[VersionNode],
    artifact_id: str,
    versions: Iterable[str],
) -> tuple[str, ...]:
    """Return every maximal known common ancestor for the requested versions."""
    selected = _graph(graph)
    try:
        validate_opaque_id(artifact_id)
        requested_set: set[str] = set()
        input_count = 0
        for version in versions:
            input_count += 1
            if input_count > _MAX_NODES:
                raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
            validate_opaque_id(version)
            requested_set.add(version)
        requested = tuple(sorted(requested_set))
        if not requested:
            raise ValueError
    except VersionGraphError:
        raise
    except Exception:
        raise VersionGraphError(VersionGraphErrorCode.INVALID) from None
    if _has_graph_issue(selected, artifact_id):
        return ()
    by_key = {node.key: node for node in selected.nodes}
    if any((artifact_id, version) not in by_key for version in requested):
        return ()
    common: set[tuple[str, str]] | None = None
    work = 0
    for version in requested:
        ancestors = _known_ancestors(by_key, (artifact_id, version))
        work += len(ancestors)
        if work > 4 * _MAX_EDGES:
            raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
        common = ancestors if common is None else common & ancestors
        if not common:
            return ()
    assert common is not None
    if not common:
        return ()
    ancestors_of_common: set[tuple[str, str]] = set()
    pending = [
        (artifact_id, parent)
        for key in common for parent in by_key[key].parents
        if (artifact_id, parent) in common
    ]
    while pending:
        key = pending.pop()
        if key in ancestors_of_common:
            continue
        ancestors_of_common.add(key)
        pending.extend(
            (artifact_id, parent) for parent in by_key[key].parents
            if (artifact_id, parent) in common
        )
    maximal = common - ancestors_of_common
    return tuple(sorted(version for _artifact, version in maximal))


def merge_requirements(
    graph: VersionGraph | Iterable[VersionNode], *, artifact_id: str,
) -> MergeRequirement | None:
    """Describe a required explicit merge while preserving every current head."""
    selected = _graph(graph)
    try:
        validate_opaque_id(artifact_id)
    except Exception:
        raise VersionGraphError(VersionGraphErrorCode.INVALID) from None
    if _has_graph_issue(selected, artifact_id):
        return MergeRequirement(
            artifact_id, (), (), _unresolved_for(selected, artifact_id), (), True,
        )
    heads = version_heads(selected, artifact_id=artifact_id)
    if len(heads) < 2:
        return None
    ancestors = common_ancestors(selected, artifact_id, heads)
    return MergeRequirement(
        artifact_id=artifact_id,
        heads=heads,
        common_ancestors=ancestors,
        unresolved_dependencies=_unresolved_for(selected, artifact_id),
        required_parents=heads,
    )


def _graph(value: VersionGraph | Iterable[VersionNode]) -> VersionGraph:
    return value if isinstance(value, VersionGraph) else build_version_graph(value)


def _known_ancestors(
    by_key: dict[tuple[str, str], VersionNode], start: tuple[str, str],
) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    pending = list(by_key[start].parents)
    while pending:
        parent = pending.pop()
        if len(found) >= _MAX_EDGES:
            raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
        key = (start[0], parent)
        if key in found or key not in by_key:
            continue
        found.add(key)
        pending.extend(by_key[key].parents)
    return found


def _cycles(
    nodes: tuple[VersionNode, ...], collisions: set[tuple[str, str]],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    by_key = {node.key: node for node in nodes if node.key not in collisions}
    state: dict[tuple[str, str], int] = {}
    found: set[tuple[tuple[str, str], ...]] = set()
    examined = 0
    cycle_members = 0
    for key in sorted(by_key):
        if state.get(key, 0) != 0:
            continue
        path: list[tuple[str, str]] = [key]
        path_index = {key: 0}
        state[key] = 1
        frames: list[tuple[tuple[str, str], object]] = [
            (key, iter(_parent_keys(by_key[key], by_key)))
        ]
        while frames:
            current, iterator = frames[-1]
            try:
                parent = next(iterator)  # type: ignore[arg-type]
                examined += 1
                if examined > _MAX_EDGES:
                    raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
            except StopIteration:
                frames.pop()
                path_index.pop(current, None)
                path.pop()
                state[current] = 2
                continue
            mark = state.get(parent, 0)
            if mark == 1:
                start = path_index[parent]
                cycle_members += len(path) - start
                if cycle_members > _MAX_EDGES:
                    raise VersionGraphError(VersionGraphErrorCode.BOUNDED)
                cycle = tuple(path[start:])
                # Version identities are unique within this cycle, so its
                # lexicographically smallest rotation starts at the least key.
                least = min(range(len(cycle)), key=cycle.__getitem__)
                found.add(cycle[least:] + cycle[:least])
            elif mark == 0:
                state[parent] = 1
                path_index[parent] = len(path)
                path.append(parent)
                frames.append((parent, iter(_parent_keys(by_key[parent], by_key))))
    return tuple(sorted(found))


def _parent_keys(
    node: VersionNode, by_key: dict[tuple[str, str], VersionNode],
) -> tuple[tuple[str, str], ...]:
    return tuple((node.artifact_id, parent) for parent in node.parents
                 if (node.artifact_id, parent) in by_key)


def _has_graph_issue(graph: VersionGraph, artifact_id: str | None) -> bool:
    def relevant(key: tuple[str, str]) -> bool:
        return artifact_id is None or key[0] == artifact_id

    return (
        any(relevant(key) for key in graph.collisions)
        or any(any(relevant(key) for key in cycle) for cycle in graph.cycles)
    )


def _unresolved_for(graph: VersionGraph, artifact_id: str) -> tuple[str, ...]:
    return tuple(sorted(parent for owner, parent in graph.missing_parents
                         if owner == artifact_id))

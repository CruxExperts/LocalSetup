from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import shutil
import traceback
import types
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator as RealDraft202012Validator

import ls.core.dependency_ledger as dependency_ledger_module
from ls.core.dependency_ledger import (
    DependencyLedgerError,
    dependency_closure,
    load_dependency_ledger,
    required_skill_closure,
)
from ls.core.selection import resolve_package_selection
from ls.core.skills import selected_skill_names


ROOT = Path(__file__).resolve().parents[2]


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)
    shutil.copy2(ROOT / "ls" / "config" / "dependency-ledger.schema.json", config)
    for name in ("ls-a", "ls-b", "ls-c"):
        skill = root / "ls" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: fixture\n---\n", encoding="utf-8")
    workflow = root / "ls" / "workflows" / "ls-workflow-demo"
    workflow.mkdir(parents=True)
    (workflow / "workflow.yaml").write_text("workflow_id: demo\n", encoding="utf-8")
    return root


def _write_ledger(root: Path, *, nodes: list[dict], edges: list[dict]) -> None:
    payload = {"schema_version": 1, "nodes": nodes, "edges": edges}
    (root / "ls" / "config" / "dependency-ledger.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _write_resource(root: Path, *, owner: str, name: str, declared_name: str | None = None) -> Path:
    resource = root / "ls" / "skills" / owner / "resources" / name
    resource.mkdir(parents=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://localsetup.invalid/fixture-resource.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["$schema", "resource_id", "owner_skill"],
        "properties": {
            "$schema": {"const": "https://localsetup.invalid/fixture-resource.schema.json"},
            "resource_id": {"type": "string"},
            "owner_skill": {"type": "string"},
        },
    }
    snapshot = {
        "$schema": "https://localsetup.invalid/fixture-resource.schema.json",
        "resource_id": declared_name or name,
        "owner_skill": owner,
    }
    schema_path = resource / "schema.json"
    snapshot_path = resource / "snapshot.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "resource_id": declared_name or name,
        "owner_skill": owner,
        "schema_path": "schema.json",
        "snapshot_path": "snapshot.json",
        "matrix_schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "evidence": [
            {
                "evidence_id": "fixture-evidence",
                "url": "https://localsetup.invalid/evidence",
                "access_date": "2026-07-17",
                "claim_scope": "model_version",
            }
        ],
    }
    (resource / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return resource


def _resource_closure_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = _fixture_repo(tmp_path)
    _write_ledger(
        root,
        nodes=[
            {"id": "skill:ls-a", "kind": "skill", "name": "ls-a"},
            {"id": "resource:model-snapshot", "kind": "resource", "name": "model-snapshot"},
        ],
        edges=[{"from": "skill:ls-a", "relation": "requires", "to": "resource:model-snapshot"}],
    )
    return root, _write_resource(root, owner="ls-a", name="model-snapshot")


def _install_resource_replacement(target: Path, replacement: Path, *, directory: bool) -> None:
    if directory:
        replacement.mkdir()
    else:
        replacement.write_text('{"replacement":"outside"}', encoding="utf-8")
    replacement.rename(target)


RESOURCE_ENTRIES = (
    (Path("manifest.json"), False),
    (Path("schema.json"), False),
    (Path("snapshot.json"), False),
    (Path("."), True),
)


def test_dependency_ledger_loads_routing_resource_and_stable_omniroute_closure() -> None:
    ledger = load_dependency_ledger(ROOT)

    assert [node.node_id for node in ledger.nodes] == [
        "resource:model-capability-matrix",
        "skill:ls-agent-capability-routing",
        "skill:ls-herdr-task-routing",
        "skill:ls-herdr-pane-layout",
        "skill:ls-herdr-provenance-drift",
        "skill:ls-herdr-remote-connect",
        "skill:ls-herdr-tasks",
        "skill:ls-omniroute",
        "skill:ls-omniroute-admin-automation",
        "skill:ls-omniroute-proxy",
        "skill:ls-omniroute-update",
    ]
    assert dependency_closure(ledger, {"skill:ls-omniroute"}) == [
        "skill:ls-omniroute",
        "skill:ls-omniroute-admin-automation",
        "skill:ls-omniroute-proxy",
        "skill:ls-omniroute-update",
    ]
    assert required_skill_closure(ROOT, ["ls-agent-capability-routing"]) == ["ls-agent-capability-routing"]
    assert required_skill_closure(ROOT, ["ls-herdr-task-routing"]) == [
        "ls-herdr-task-routing",
        "ls-herdr-pane-layout",
        "ls-herdr-provenance-drift",
        "ls-herdr-remote-connect",
        "ls-herdr-tasks",
    ]


@pytest.mark.parametrize(
    ("nodes", "edges", "message"),
    [
        (
            [{"id": "skill:ls-a", "kind": "skill", "name": "ls-a"}] * 2,
            [],
            "duplicate node IDs",
        ),
        (
            [{"id": "skill:ls-a", "kind": "skill", "name": "ls-a"}],
            [{"from": "skill:ls-a", "relation": "requires", "to": "skill:ls-missing"}],
            "dangling node",
        ),
        (
            [{"id": "skill:ls-a", "kind": "skill", "name": "ls-a"}],
            [{"from": "skill:ls-a", "relation": "requires", "to": "skill:ls-a"}],
            "self-referential",
        ),
        (
            [
                {"id": "skill:ls-a", "kind": "skill", "name": "ls-a"},
                {"id": "skill:ls-b", "kind": "skill", "name": "ls-b"},
            ],
            [
                {"from": "skill:ls-a", "relation": "requires", "to": "skill:ls-b"},
                {"from": "skill:ls-b", "relation": "requires", "to": "skill:ls-a"},
            ],
            "requires cycle",
        ),
    ],
)
def test_dependency_ledger_rejects_invalid_graphs(tmp_path: Path, nodes: list[dict], edges: list[dict], message: str) -> None:
    root = _fixture_repo(tmp_path)
    _write_ledger(root, nodes=nodes, edges=edges)

    with pytest.raises(DependencyLedgerError, match=message):
        load_dependency_ledger(root)


def test_dependency_ledger_rejects_mismatched_id_and_preserves_typed_resource(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    _write_ledger(
        root,
        nodes=[{"id": "skill:ls-a", "kind": "skill", "name": "ls-b"}],
        edges=[],
    )
    with pytest.raises(DependencyLedgerError, match="does not match"):
        load_dependency_ledger(root)

    _write_ledger(
        root,
        nodes=[{"id": "resource:not-declared", "kind": "resource", "name": "not-declared"}],
        edges=[],
    )
    ledger = load_dependency_ledger(root)
    assert ledger.nodes[0].node_id == "resource:not-declared"


def test_dependency_ledger_retains_cross_kind_closure_and_selection_rejects_materialization(
    tmp_path: Path,
) -> None:
    root = _fixture_repo(tmp_path)
    nodes = [
        {"id": "skill:ls-a", "kind": "skill", "name": "ls-a"},
        {
            "id": "workflow:ls-workflow-demo",
            "kind": "workflow",
            "name": "ls-workflow-demo",
        },
        {
            "id": "resource:model-snapshot",
            "kind": "resource",
            "name": "model-snapshot",
        },
    ]
    edges = [
        {
            "from": "skill:ls-a",
            "relation": "requires",
            "to": "workflow:ls-workflow-demo",
        },
        {
            "from": "workflow:ls-workflow-demo",
            "relation": "requires",
            "to": "resource:model-snapshot",
        },
    ]
    _write_ledger(root, nodes=nodes, edges=edges)

    ledger = load_dependency_ledger(root)
    assert dependency_closure(ledger, {"skill:ls-a"}) == [
        "resource:model-snapshot",
        "skill:ls-a",
        "workflow:ls-workflow-demo",
    ]
    with pytest.raises(
        DependencyLedgerError,
        match="unsupported dependency materialization: resource:model-snapshot",
    ):
        required_skill_closure(root, ["ls-a"])


def test_selected_skill_resource_closure_requires_one_safe_direct_manifest(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    _write_ledger(
        root,
        nodes=[
            {"id": "skill:ls-a", "kind": "skill", "name": "ls-a"},
            {"id": "resource:model-snapshot", "kind": "resource", "name": "model-snapshot"},
        ],
        edges=[{"from": "skill:ls-a", "relation": "requires", "to": "resource:model-snapshot"}],
    )
    _write_resource(root, owner="ls-a", name="model-snapshot")
    assert required_skill_closure(root, ["ls-a"]) == ["ls-a"]

    _write_resource(root, owner="ls-a", name="duplicate", declared_name="model-snapshot")
    with pytest.raises(DependencyLedgerError, match="ownership does not match resource tree"):
        required_skill_closure(root, ["ls-a"])


def test_selected_skill_resource_closure_rejects_missing_and_symlinked_resource_inputs(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    _write_ledger(
        root,
        nodes=[
            {"id": "skill:ls-a", "kind": "skill", "name": "ls-a"},
            {"id": "resource:model-snapshot", "kind": "resource", "name": "model-snapshot"},
        ],
        edges=[{"from": "skill:ls-a", "relation": "requires", "to": "resource:model-snapshot"}],
    )
    with pytest.raises(DependencyLedgerError, match="owner tree"):
        required_skill_closure(root, ["ls-a"])

    resource = _write_resource(root, owner="ls-a", name="model-snapshot")
    schema = resource / "schema.json"
    schema.rename(resource / "schema-real.json")
    schema.symlink_to(resource / "schema-real.json")
    with pytest.raises(DependencyLedgerError, match="missing or unsafe"):
        required_skill_closure(root, ["ls-a"])


@pytest.mark.parametrize(("relative", "directory"), RESOURCE_ENTRIES)
@pytest.mark.parametrize("replacement_kind", ("symlink", "ordinary"))
def test_materializer_rejects_preopen_resource_replacement_without_consuming_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
    directory: bool,
    replacement_kind: str,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    target = resource if directory else resource / relative
    original_open = dependency_ledger_module.os.open
    original_read = dependency_ledger_module.os.read
    backup = target.with_name(f"{target.name}.original")
    outside = target.with_name(f"{target.name}.outside")
    replacement_inode: int | None = None
    swapped = False

    def swapping_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal replacement_inode, swapped
        is_target = name == target.name and bool(flags & dependency_ledger_module.os.O_DIRECTORY) is directory
        if not swapped and is_target:
            swapped = True
            target.rename(backup)
            if replacement_kind == "symlink":
                if directory:
                    outside.mkdir()
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    outside.write_text('{"outside":"replacement"}', encoding="utf-8")
                    target.symlink_to(outside)
                replacement_inode = os.stat(outside).st_ino
            else:
                _install_resource_replacement(target, outside, directory=directory)
                replacement_inode = os.stat(target).st_ino
        return original_open(name, flags, mode, dir_fd=dir_fd)

    def guarded_read(fd, size):
        assert replacement_inode is None or os.fstat(fd).st_ino != replacement_inode
        return original_read(fd, size)

    monkeypatch.setattr(dependency_ledger_module.os, "open", swapping_open)
    monkeypatch.setattr(dependency_ledger_module.os, "read", guarded_read)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    assert swapped
    assert str(target) not in str(captured.value)
    assert str(outside) not in str(captured.value)


@pytest.mark.parametrize(("relative", "directory"), RESOURCE_ENTRIES)
@pytest.mark.parametrize("replacement_kind", ("symlink", "ordinary"))
def test_materializer_keeps_held_resource_fd_after_postopen_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
    directory: bool,
    replacement_kind: str,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    target = resource if directory else resource / relative
    original_open = dependency_ledger_module.os.open
    original_read = dependency_ledger_module.os.read
    backup = target.with_name(f"{target.name}.original")
    outside = target.with_name(f"{target.name}.outside")
    original_inode = target.stat().st_ino
    replacement_inode: int | None = None
    held_target_fd: int | None = None
    target_read_seen = False
    swapped = False

    def swapping_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal held_target_fd, replacement_inode, swapped
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        is_target = name == target.name and bool(flags & dependency_ledger_module.os.O_DIRECTORY) is directory
        if not swapped and is_target:
            swapped = True
            held_target_fd = fd
            target.rename(backup)
            if replacement_kind == "symlink":
                if directory:
                    outside.mkdir()
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    outside.write_text('{"outside":"replacement"}', encoding="utf-8")
                    target.symlink_to(outside)
                replacement_inode = os.stat(outside).st_ino
            else:
                _install_resource_replacement(target, outside, directory=directory)
                replacement_inode = os.stat(target).st_ino
        return fd

    def guarded_read(fd, size):
        nonlocal target_read_seen
        inode = os.fstat(fd).st_ino
        assert inode != replacement_inode
        if fd == held_target_fd and not directory and inode == original_inode:
            target_read_seen = True
        return original_read(fd, size)

    monkeypatch.setattr(dependency_ledger_module.os, "open", swapping_open)
    monkeypatch.setattr(dependency_ledger_module.os, "read", guarded_read)
    assert required_skill_closure(root, ["ls-a"]) == ["ls-a"]
    assert swapped
    assert directory or target_read_seen


def test_materializer_static_reader_bounds_reads_and_closes_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    parent_fd = os.open(resource, os.O_RDONLY | os.O_DIRECTORY)
    original_read = dependency_ledger_module.os.read
    original_close = dependency_ledger_module.os.close
    closed: list[int] = []
    try:
        with pytest.raises(DependencyLedgerError) as malformed:
            dependency_ledger_module._read_resource_file(parent_fd, "\x00", label="malformed")
        assert str(resource) not in str(malformed.value)

        overflow = resource / "overflow.json"
        overflow.write_bytes(b"x" * 5)
        monkeypatch.setattr(dependency_ledger_module, "MAX_STATIC_RESOURCE_BYTES", 4)
        monkeypatch.setattr(dependency_ledger_module.os, "read", lambda fd, size: pytest.fail("overflow target was read"))
        with pytest.raises(DependencyLedgerError):
            dependency_ledger_module._read_resource_file(parent_fd, "overflow.json", label="overflow")

        chunked = resource / "chunked.json"
        chunked.write_bytes(b"payload")
        monkeypatch.setattr(dependency_ledger_module, "MAX_STATIC_RESOURCE_BYTES", 32)
        monkeypatch.setattr(dependency_ledger_module.os, "read", lambda fd, size: original_read(fd, min(size, 2)))
        monkeypatch.setattr(
            dependency_ledger_module.os,
            "close",
            lambda fd: (closed.append(fd), original_close(fd))[1],
        )
        assert dependency_ledger_module._read_resource_file(parent_fd, "chunked.json", label="chunked") == b"payload"
        assert closed

        monkeypatch.setattr(dependency_ledger_module.os, "read", lambda fd, size: b"")
        with pytest.raises(DependencyLedgerError):
            dependency_ledger_module._read_resource_file(parent_fd, "chunked.json", label="chunked")

        monkeypatch.setattr(
            dependency_ledger_module.os,
            "read",
            lambda fd, size: b"x" if size == 1 else original_read(fd, size),
        )
        with pytest.raises(DependencyLedgerError):
            dependency_ledger_module._read_resource_file(parent_fd, "chunked.json", label="chunked")
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("failure_name", (None, "manifest.json"))
def test_materializer_closes_every_resource_descriptor_on_success_and_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_name: str | None,
) -> None:
    root, _ = _resource_closure_fixture(tmp_path)
    original_open = dependency_ledger_module.os.open
    original_close = dependency_ledger_module.os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracing_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == failure_name:
            raise OSError("injected open failure")
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def tracing_close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(dependency_ledger_module.os, "open", tracing_open)
    monkeypatch.setattr(dependency_ledger_module.os, "close", tracing_close)
    if failure_name is None:
        assert required_skill_closure(root, ["ls-a"]) == ["ls-a"]
    else:
        with pytest.raises(DependencyLedgerError):
            required_skill_closure(root, ["ls-a"])
    assert set(opened) <= set(closed)


def _assert_sanitized_materializer_error(error: BaseException, root: Path, sentinel: str) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert str(root) not in rendered
    assert sentinel not in rendered


def _install_materializer_descriptor_failure(monkeypatch: pytest.MonkeyPatch, capability: str, sentinel: str) -> None:
    original_os = dependency_ledger_module.os
    proxy = types.SimpleNamespace(
        stat=original_os.stat,
        open=original_os.open,
        fstat=original_os.fstat,
        read=original_os.read,
        close=original_os.close,
        listdir=original_os.listdir,
        O_RDONLY=original_os.O_RDONLY,
        O_DIRECTORY=original_os.O_DIRECTORY,
        O_NOFOLLOW=original_os.O_NOFOLLOW,
    )
    monkeypatch.setattr(dependency_ledger_module, "os", proxy)
    original_stat = proxy.stat
    original_fstat = proxy.fstat
    original_read = proxy.read
    original_close = proxy.close
    if capability == "stat":
        def failing_stat(*args, **kwargs):
            if "dir_fd" in kwargs:
                raise NotImplementedError(sentinel)
            return original_stat(*args, **kwargs)

        monkeypatch.setattr(proxy, "stat", failing_stat)
    elif capability == "open":
        monkeypatch.setattr(
            proxy,
            "open",
            lambda *args, **kwargs: (_ for _ in ()).throw(NotImplementedError(sentinel)),
        )
    elif capability == "fstat":
        def failing_fstat(fd):
            original_fstat(fd)
            raise NotImplementedError(sentinel)

        monkeypatch.setattr(proxy, "fstat", failing_fstat)
    elif capability == "read":
        monkeypatch.setattr(
            proxy,
            "read",
            lambda *args, **kwargs: (_ for _ in ()).throw(NotImplementedError(sentinel)),
        )
    elif capability == "close":
        read_started = False

        def marking_read(fd, size):
            nonlocal read_started
            read_started = True
            return original_read(fd, size)

        def failing_close(fd):
            if read_started:
                raise NotImplementedError(sentinel)
            return original_close(fd)

        monkeypatch.setattr(proxy, "read", marking_read)
        monkeypatch.setattr(proxy, "close", failing_close)
    elif capability == "O_NOFOLLOW":
        monkeypatch.delattr(proxy, "O_NOFOLLOW", raising=False)
    elif capability == "O_DIRECTORY":
        monkeypatch.delattr(proxy, "O_DIRECTORY", raising=False)
    elif capability == "listdir":
        monkeypatch.setattr(
            proxy,
            "listdir",
            lambda *args, **kwargs: (_ for _ in ()).throw(NotImplementedError(sentinel)),
        )
    else:  # pragma: no cover - parameter contract
        raise AssertionError(capability)


@pytest.mark.parametrize("capability", ("stat", "open", "fstat", "read", "close", "O_NOFOLLOW", "O_DIRECTORY", "listdir"))
def test_materializer_normalizes_every_descriptor_capability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    root, _ = _resource_closure_fixture(tmp_path)
    sentinel = f"materializer-private-{capability}"
    _install_materializer_descriptor_failure(monkeypatch, capability, sentinel)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    _assert_sanitized_materializer_error(captured.value, root, sentinel)


def test_materializer_root_fstat_failure_closes_every_opened_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _resource_closure_fixture(tmp_path)
    original_os = dependency_ledger_module.os
    proxy = types.SimpleNamespace(
        open=original_os.open,
        fstat=original_os.fstat,
        close=original_os.close,
        stat=original_os.stat,
        read=original_os.read,
        listdir=original_os.listdir,
        O_RDONLY=original_os.O_RDONLY,
        O_DIRECTORY=original_os.O_DIRECTORY,
        O_NOFOLLOW=original_os.O_NOFOLLOW,
    )
    monkeypatch.setattr(dependency_ledger_module, "os", proxy)
    original_open = proxy.open
    original_fstat = proxy.fstat
    original_close = proxy.close
    opened: list[int] = []
    closed: list[int] = []

    def tracing_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def failing_fstat(fd):
        original_fstat(fd)
        raise NotImplementedError("materializer-root-fstat-private")

    def tracing_close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(proxy, "open", tracing_open)
    monkeypatch.setattr(proxy, "fstat", failing_fstat)
    monkeypatch.setattr(proxy, "close", tracing_close)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    _assert_sanitized_materializer_error(captured.value, root, "materializer-root-fstat-private")
    assert Counter(opened) == Counter(closed)


@pytest.mark.parametrize("target_name", ("ls", "skills", "ls-a", "resources", "manifest.json"))
def test_materializer_component_open_failure_closes_every_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    root, _ = _resource_closure_fixture(tmp_path)
    original_os = dependency_ledger_module.os
    proxy = types.SimpleNamespace(
        open=original_os.open,
        fstat=original_os.fstat,
        close=original_os.close,
        stat=original_os.stat,
        read=original_os.read,
        listdir=original_os.listdir,
        O_RDONLY=original_os.O_RDONLY,
        O_DIRECTORY=original_os.O_DIRECTORY,
        O_NOFOLLOW=original_os.O_NOFOLLOW,
    )
    monkeypatch.setattr(dependency_ledger_module, "os", proxy)
    original_open = proxy.open
    original_fstat = proxy.fstat
    original_close = proxy.close
    opened: list[int] = []
    closed: list[int] = []
    target_fd: int | None = None

    def tracing_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal target_fd
        fd = original_open(name, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        if name == target_name and target_fd is None:
            target_fd = fd
        return fd

    def failing_fstat(fd):
        result = original_fstat(fd)
        if fd == target_fd:
            raise NotImplementedError(f"component-private-{target_name}")
        return result

    def tracing_close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(proxy, "open", tracing_open)
    monkeypatch.setattr(proxy, "fstat", failing_fstat)
    monkeypatch.setattr(proxy, "close", tracing_close)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    assert target_fd is not None
    _assert_sanitized_materializer_error(captured.value, root, f"component-private-{target_name}")
    assert Counter(opened) == Counter(closed)


def _read_resource_manifest(resource: Path) -> dict:
    return json.loads((resource / "manifest.json").read_text(encoding="utf-8"))


def _write_resource_manifest(resource: Path, manifest: dict) -> None:
    (resource / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_manifest",
        "wrong_owner",
        "unsafe_schema_path",
        "unsafe_snapshot_path",
        "schema_digest_mismatch",
        "snapshot_digest_mismatch",
    ),
)
def test_materializer_rejects_each_closure_contract_mutation_without_leak(
    tmp_path: Path,
    mutation: str,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    sentinel = f"closure-private-{mutation}"
    manifest = _read_resource_manifest(resource)
    if mutation == "missing_manifest":
        (resource / "manifest.json").unlink()
    elif mutation == "wrong_owner":
        manifest["owner_skill"] = sentinel
        _write_resource_manifest(resource, manifest)
    elif mutation == "unsafe_schema_path":
        manifest["schema_path"] = f"../{sentinel}"
        _write_resource_manifest(resource, manifest)
    elif mutation == "unsafe_snapshot_path":
        manifest["snapshot_path"] = f"../{sentinel}"
        _write_resource_manifest(resource, manifest)
    elif mutation == "schema_digest_mismatch":
        manifest["matrix_schema_sha256"] = "0" * 64
        _write_resource_manifest(resource, manifest)
    elif mutation == "snapshot_digest_mismatch":
        manifest["snapshot_sha256"] = "0" * 64
        _write_resource_manifest(resource, manifest)
    else:  # pragma: no cover - parameter contract
        raise AssertionError(mutation)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    _assert_sanitized_materializer_error(captured.value, root, sentinel)


def test_materializer_invalid_captured_schema_reaches_check_schema_after_digest_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    schema_path = resource / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["type"] = "not-a-json-schema-type"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    manifest = _read_resource_manifest(resource)
    manifest["matrix_schema_sha256"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    _write_resource_manifest(resource, manifest)
    original_load = dependency_ledger_module._load_resource_json
    captured_objects: dict[str, dict] = {}
    captured_bytes: dict[str, bytes] = {}
    events: list[str] = []

    def capturing_load(parent_fd, name, *, label):
        value, data = original_load(parent_fd, name, label=label)
        if label in {"schema", "snapshot"}:
            captured_objects[label] = value
            captured_bytes[label] = data
        return value, data

    original_hashlib = dependency_ledger_module.hashlib

    class ComparedDigest(str):
        def __new__(cls, value: str, label: str):
            instance = super().__new__(cls, value)
            instance.label = label
            return instance

        def __eq__(self, other):
            result = super().__eq__(other)
            events.append(f"{self.label}-digest")
            return result

        def __ne__(self, other):
            result = super().__ne__(other)
            events.append(f"{self.label}-digest")
            return result

    class ForwardingDigest:
        def __init__(self, digest, label: str):
            self._digest = digest
            self._label = label

        def hexdigest(self):
            return ComparedDigest(self._digest.hexdigest(), self._label)

    def tracing_sha256(data):
        if data is captured_bytes.get("schema"):
            label = "schema"
        elif data is captured_bytes.get("snapshot"):
            label = "snapshot"
        else:
            label = "unexpected"
        return ForwardingDigest(original_hashlib.sha256(data), label)

    class ValidatorSpy:
        checked: list[dict] = []
        constructed: list[dict] = []
        iterated: list[dict] = []

        @staticmethod
        def check_schema(value):
            ValidatorSpy.checked.append(value)
            events.append("check-schema")
            return RealDraft202012Validator.check_schema(value)

        def __init__(self, value):
            ValidatorSpy.constructed.append(value)
            events.append("construct")
            self._delegate = RealDraft202012Validator(value)

        def iter_errors(self, value):
            ValidatorSpy.iterated.append(value)
            events.append("iter-errors")
            return self._delegate.iter_errors(value)

    monkeypatch.setattr(dependency_ledger_module, "_load_resource_json", capturing_load)
    monkeypatch.setattr(
        dependency_ledger_module,
        "hashlib",
        types.SimpleNamespace(sha256=tracing_sha256),
    )
    monkeypatch.setattr(dependency_ledger_module, "Draft202012Validator", ValidatorSpy)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    _assert_sanitized_materializer_error(captured.value, root, "not-a-json-schema-type")
    assert events == ["schema-digest", "snapshot-digest", "check-schema"]
    assert len(ValidatorSpy.checked) == 1
    assert ValidatorSpy.checked[0] is captured_objects["schema"]
    assert ValidatorSpy.constructed == []
    assert ValidatorSpy.iterated == []


def test_materializer_invalid_captured_snapshot_reaches_iter_errors_after_digest_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, resource = _resource_closure_fixture(tmp_path)
    snapshot_path = resource / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["unexpected"] = "snapshot-private-sentinel"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    manifest = _read_resource_manifest(resource)
    manifest["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    _write_resource_manifest(resource, manifest)
    original_load = dependency_ledger_module._load_resource_json
    captured_objects: dict[str, dict] = {}
    captured_bytes: dict[str, bytes] = {}
    events: list[str] = []

    def capturing_load(parent_fd, name, *, label):
        value, data = original_load(parent_fd, name, label=label)
        if label in {"schema", "snapshot"}:
            captured_objects[label] = value
            captured_bytes[label] = data
        return value, data

    original_hashlib = dependency_ledger_module.hashlib

    class ComparedDigest(str):
        def __new__(cls, value: str, label: str):
            instance = super().__new__(cls, value)
            instance.label = label
            return instance

        def __eq__(self, other):
            result = super().__eq__(other)
            events.append(f"{self.label}-digest")
            return result

        def __ne__(self, other):
            result = super().__ne__(other)
            events.append(f"{self.label}-digest")
            return result

    class ForwardingDigest:
        def __init__(self, digest, label: str):
            self._digest = digest
            self._label = label

        def hexdigest(self):
            return ComparedDigest(self._digest.hexdigest(), self._label)

    def tracing_sha256(data):
        if data is captured_bytes.get("schema"):
            label = "schema"
        elif data is captured_bytes.get("snapshot"):
            label = "snapshot"
        else:
            label = "unexpected"
        return ForwardingDigest(original_hashlib.sha256(data), label)

    class ValidatorSpy:
        checked: list[dict] = []
        constructed: list[dict] = []
        iterated: list[dict] = []
        issues: list[object] = []

        @staticmethod
        def check_schema(value):
            ValidatorSpy.checked.append(value)
            events.append("check-schema")
            return RealDraft202012Validator.check_schema(value)

        def __init__(self, value):
            ValidatorSpy.constructed.append(value)
            events.append("construct")
            self._delegate = RealDraft202012Validator(value)

        def iter_errors(self, value):
            ValidatorSpy.iterated.append(value)
            events.append("iter-errors")
            issues = list(self._delegate.iter_errors(value))
            ValidatorSpy.issues.extend(issues)
            return iter(issues)

    monkeypatch.setattr(dependency_ledger_module, "_load_resource_json", capturing_load)
    monkeypatch.setattr(
        dependency_ledger_module,
        "hashlib",
        types.SimpleNamespace(sha256=tracing_sha256),
    )
    monkeypatch.setattr(dependency_ledger_module, "Draft202012Validator", ValidatorSpy)
    with pytest.raises(DependencyLedgerError) as captured:
        required_skill_closure(root, ["ls-a"])
    _assert_sanitized_materializer_error(captured.value, root, "snapshot-private-sentinel")
    assert events == ["schema-digest", "snapshot-digest", "check-schema", "construct", "iter-errors"]
    assert len(ValidatorSpy.checked) == 1
    assert ValidatorSpy.checked[0] is captured_objects["schema"]
    assert len(ValidatorSpy.constructed) == 1
    assert ValidatorSpy.constructed[0] is captured_objects["schema"]
    assert len(ValidatorSpy.iterated) == 1
    assert ValidatorSpy.iterated[0] is captured_objects["snapshot"]
    assert ValidatorSpy.issues


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 1, "nodes": []}, "unsupported or missing fields"),
        (
            {"schema_version": "1", "nodes": [], "edges": []},
            "schema_version must be 1",
        ),
        (
            {"schema_version": 1, "nodes": {}, "edges": []},
            "nodes and edges must be lists",
        ),
        (
            {"schema_version": 1, "nodes": [{"id": "skill:ls-a"}], "edges": []},
            "unsupported or missing fields",
        ),
    ],
)
def test_dependency_ledger_manual_invariants_are_stable_without_jsonschema(
    tmp_path: Path,
    payload: dict,
    message: str,
) -> None:
    root = _fixture_repo(tmp_path)
    path = root / "ls" / "config" / "dependency-ledger.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(DependencyLedgerError, match=message):
        load_dependency_ledger(root, require_jsonschema=False)


def test_dependency_ledger_requires_readable_schema_with_sanitized_error(tmp_path: Path) -> None:
    root = _fixture_repo(tmp_path)
    _write_ledger(
        root,
        nodes=[{"id": "skill:ls-a", "kind": "skill", "name": "ls-a"}],
        edges=[],
    )
    schema = root / "ls" / "config" / "dependency-ledger.schema.json"
    schema.unlink()

    with pytest.raises(
        DependencyLedgerError,
        match="dependency ledger schema is missing or unsafe",
    ) as captured:
        load_dependency_ledger(root, require_jsonschema=False)
    assert str(root) not in str(captured.value)


def test_pack_and_explicit_selection_restore_hard_omniroute_dependencies() -> None:
    assert {"ls-omniroute", "ls-omniroute-admin-automation", "ls-omniroute-proxy", "ls-omniroute-update"} <= set(
        selected_skill_names(ROOT, ["omniroute"])
    )

    selected = resolve_package_selection(
        ROOT,
        preset="custom",
        skills=["ls-omniroute"],
        exclude_skills=["ls-omniroute-proxy"],
    )
    assert selected.skills == [
        "ls-omniroute",
        "ls-omniroute-admin-automation",
        "ls-omniroute-proxy",
        "ls-omniroute-update",
    ]

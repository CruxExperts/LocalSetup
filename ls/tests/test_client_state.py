from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import shutil
import socket
import stat
import subprocess
from threading import BrokenBarrierError
import time

import pytest

from ls.core.client_state import (
    ClientStateError,
    allocate_artifact,
    apply_git_exclude,
    parse_artifact_name,
    plan_git_exclude,
    prepare_artifact_request,
    probe_git_context,
    resolve_state_location,
    verify_artifact,
)
from ls.core.client_state import locator
from ls.core.client_state import artifacts, git_exclude


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "ls" / "config" / "client-state-artifact.schema.json"
FD_ROOT = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
REAL_ALLOCATION_LOCK_HOLD_SECONDS = 2.05
PROCESS_STARTUP_SECONDS = 20
PROCESS_ALLOCATION_SECONDS = 5
ACCEPTED_RELATIVE_PATHS = (
    "foo",
    "foo/bar",
    "deeply/nested/artifact/path",
    "café/文件",
    "folder name/file name.md",
    "a" * 512,
)
REJECTED_RELATIVE_PATHS = (
    ".",
    "../private",
    "folder\\secret",
    "C:/private",
    "//server/share",
    "folder//file",
    "folder/./file",
    "line\nsecret",
    "foo/",
    "a" * 513,
)


@pytest.fixture(autouse=True)
def deterministic_safe_creation_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "LocalSetup Test")
    git(path, "config", "user.email", "test@localsetup.invalid")
    (path / "tracked").write_text("one\n", encoding="utf-8")
    git(path, "add", "tracked")
    git(path, "commit", "-qm", "test fixture")
    return path


def registry_root(path: Path) -> Path:
    config = path / "ls" / "config"
    config.mkdir(parents=True)
    shutil.copy2(ROOT / "ls" / "config" / "clients.yaml", config / "clients.yaml")
    shutil.copy2(ROOT / "ls" / "config" / "clients.schema.json", config / "clients.schema.json")
    return path


def artifact_options(content: object = b"checkpoint\n") -> dict:
    return {
        "content": content,
        "purpose": "w14-boundary",
        "extension": "md",
        "kind": "restart-artifact",
        "schema": "restart-v1",
        "producer": "controller",
    }


def exclude_bytes(repo: Path) -> bytes:
    return (repo / ".git" / "info" / "exclude").read_bytes()


def descriptor_count() -> int:
    return len(list(FD_ROOT.iterdir()))


def descriptor_target(fd: int) -> str:
    return os.readlink(FD_ROOT / str(fd))


def stat_with_uid(result: os.stat_result, uid: int) -> os.stat_result:
    fields = list(result)
    fields[4] = uid
    return os.stat_result(fields)


def stat_with_dev(result: os.stat_result, device: int) -> os.stat_result:
    fields = list(result)
    fields[2] = device
    return os.stat_result(fields)


def stat_with_mode(result: os.stat_result, mode: int) -> os.stat_result:
    fields = list(result)
    fields[0] = stat.S_IFMT(result.st_mode) | mode
    return os.stat_result(fields)


def concurrent_allocate_worker(
    cwd: str,
    home: str,
    scope: str,
    codex_home: str | None,
    barrier,
    results,
) -> None:
    try:
        if codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = codex_home
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=Path(cwd), home=Path(home), scope=scope
        )
        recover = artifacts._recover_pending

        def hold_lock(directory_fd: int) -> None:
            time.sleep(0.1)
            recover(directory_fd)

        artifacts._recover_pending = hold_lock
        barrier.wait(timeout=PROCESS_STARTUP_SECONDS)
        allocated = allocate_artifact(
            location,
            content=b"concurrent\n",
            purpose="same-time",
            extension="md",
            kind="restart-artifact",
            schema="restart-v1",
            producer="controller",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
        results.put(("ok", allocated["artifact"]))
    except BaseException as exc:
        barrier.abort()
        results.put(("error", type(exc).__name__, getattr(exc, "code", None)))


def reap_allocation_processes(processes, *queues) -> None:
    for process in processes:
        if process.pid is not None and process.is_alive():
            process.terminate()
    for process in processes:
        if process.pid is None:
            continue
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
    for results in queues:
        results.close()
        results.join_thread()


def concurrent_allocation_outcomes(
    cwd: Path, home: Path, scope: str, codex_homes: tuple[str | None, str | None]
) -> tuple[bool, list[int | None], list[tuple]]:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=concurrent_allocate_worker,
            args=(str(cwd), str(home), scope, codex_home, barrier, results),
        )
        for codex_home in codex_homes
    ]
    try:
        startup_deadline = time.monotonic() + PROCESS_STARTUP_SECONDS
        for process in processes:
            process.start()
        try:
            barrier.wait(timeout=max(0, startup_deadline - time.monotonic()))
            ready = True
        except BrokenBarrierError:
            ready = False
        deadline = time.monotonic() + PROCESS_ALLOCATION_SECONDS if ready else startup_deadline
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
        exitcodes = [process.exitcode for process in processes]
        outcomes = []
        for _ in processes:
            try:
                outcomes.append(results.get(timeout=0.25))
            except Empty:
                break
        return ready, exitcodes, outcomes
    finally:
        barrier.abort()
        reap_allocation_processes(processes, results)


def held_allocation_lock_worker(
    cwd: str,
    home: str,
    scope: str,
    codex_home: str | None,
    lock_acquired,
    contender_attempted,
    results,
) -> None:
    if codex_home is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = codex_home
    directory_fd: int | None = None
    lock_fd: int | None = None
    try:
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=Path(cwd), home=Path(home), scope=scope
        )
        directory_fd = artifacts._open_location_directory(location, create=True)
        lock_fd = artifacts._open_owned_lock(directory_fd, artifacts._ALLOCATION_LOCK)
        artifacts._acquire_allocation_lock(lock_fd)
        lock_acquired.set()
        if not contender_attempted.wait(timeout=PROCESS_STARTUP_SECONDS):
            raise RuntimeError("contender never attempted the held allocation lock")
        time.sleep(REAL_ALLOCATION_LOCK_HOLD_SECONDS)
        results.put(("ok",))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, getattr(exc, "code", None)))
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def allocation_contender_worker(
    cwd: str,
    home: str,
    scope: str,
    codex_home: str | None,
    contender_attempted,
    results,
) -> None:
    if codex_home is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = codex_home
    original_flock = artifacts.fcntl.flock

    def report_attempt(*args) -> None:
        contender_attempted.set()
        original_flock(*args)

    artifacts.fcntl.flock = report_attempt
    try:
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=Path(cwd), home=Path(home), scope=scope
        )
        allocated = allocate_artifact(
            location,
            content=b"concurrent\n",
            purpose="same-time",
            extension="md",
            kind="restart-artifact",
            schema="restart-v1",
            producer="controller",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
        results.put(("ok", allocated["artifact"]))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, getattr(exc, "code", None)))
    finally:
        artifacts.fcntl.flock = original_flock


def test_locator_selects_nested_repo_and_registry_root(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    nested = repo / "one" / "two"
    nested.mkdir(parents=True)
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=nested, home=tmp_path / "home"
    )
    assert location.scope == "repo"
    assert location.root == repo / ".codex" / "state"
    assert location.state_path == ".codex/state"
    assert location.git and location.git.root == repo


def test_locator_uses_worktree_git_path(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-qb", "fixture-worktree", str(worktree))
    location = resolve_state_location(
        ROOT, "claude-code/claude-code-cli", cwd=worktree, home=tmp_path / "home"
    )
    assert location.root == worktree / ".claude" / "state"
    assert location.git and location.git.exclude_path.name == "exclude"
    assert location.git.exclude_path.is_absolute()


def test_locator_honors_submodule_root(tmp_path: Path) -> None:
    child = repository(tmp_path / "child")
    parent = repository(tmp_path / "parent")
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(parent), "submodule", "add", "-q", str(child), "module"],
        check=True,
    )
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=parent / "module", home=tmp_path / "home"
    )
    assert location.git and location.git.root == parent / "module"
    assert location.root == parent / "module" / ".codex" / "state"


@pytest.mark.parametrize("suffix", [" ", "\t"])
@pytest.mark.parametrize("with_stripped_sibling", [False, True])
def test_git_root_trailing_whitespace_is_preserved_end_to_end(
    tmp_path: Path, suffix: str, with_stripped_sibling: bool
) -> None:
    actual = repository(tmp_path / f"repo{suffix}")
    sibling = repository(tmp_path / "repo") if with_stripped_sibling else tmp_path / "repo"
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=actual, home=tmp_path / "home"
    )
    assert location.git and location.git.root == actual
    allocated = allocate_artifact(location, **artifact_options())
    current = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=actual, home=tmp_path / "home"
    )
    assert verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)["ok"]
    assert (actual / ".codex" / "state" / allocated["artifact"]).is_file()
    assert not (sibling / ".codex" / "state").exists()
    if with_stripped_sibling:
        assert b"/.codex/state/" not in exclude_bytes(sibling)


def test_git_root_terminal_carriage_return_is_typed_and_never_redirected(
    tmp_path: Path
) -> None:
    actual = repository(tmp_path / "repo\r")
    sibling = repository(tmp_path / "repo")
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(ROOT, "codex/codex-cli", cwd=actual, home=tmp_path / "home")
    assert failure.value.code == "git_probe_failed"
    assert not (actual / ".codex" / "state").exists()
    assert not (sibling / ".codex" / "state").exists()
    assert b"/.codex/state/" not in exclude_bytes(sibling)


def test_git_root_undecodable_byte_is_preserved_without_replacement_sibling_redirect(
    tmp_path: Path
) -> None:
    actual = repository(tmp_path / os.fsdecode(b"repo-\xff"))
    sibling = repository(tmp_path / "repo-\ufffd")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=actual, home=tmp_path / "home"
    )
    assert location.git and os.fsencode(location.git.root) == os.fsencode(actual)
    allocated = allocate_artifact(location, **artifact_options())
    assert (actual / ".codex" / "state" / allocated["artifact"]).is_file()
    assert not (sibling / ".codex" / "state").exists()
    assert b"/.codex/state/" not in exclude_bytes(sibling)


@pytest.mark.parametrize(
    "record",
    [
        b"/tmp/repo",
        b"\n",
        b"relative/repo\n",
        b"/tmp/repo\x00evil\n",
        b"/tmp/one\n/tmp/two\n",
        b"/tmp/repo\r\n",
    ],
)
def test_path_valued_git_records_require_one_clean_lf_delimited_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bytes
) -> None:
    result = subprocess.CompletedProcess(["git"], 0, record, b"")
    monkeypatch.setattr(locator, "_git_bytes", lambda *_args: result)
    with pytest.raises(ClientStateError) as failure:
        locator._required_git_path(tmp_path, "rev-parse", "--show-toplevel")
    assert failure.value.code == "git_probe_failed"


def test_relative_git_path_record_maps_per_consumer_without_wrong_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    (tmp_path / "relative").mkdir()
    wrong = repository(tmp_path / "relative" / "repo")
    repo_before = exclude_bytes(repo)
    wrong_before = exclude_bytes(wrong)
    relative = subprocess.CompletedProcess(["git"], 0, b"relative/repo\n", b"")
    monkeypatch.setattr(locator, "_git_bytes", lambda *_args: relative)

    with pytest.raises(ClientStateError) as locator_failure:
        probe_git_context(repo)
    assert locator_failure.value.code == "git_probe_failed"
    with pytest.raises(ClientStateError) as exclude_failure:
        git_exclude._resolved_exclude(repo)
    assert exclude_failure.value.code == "git_ignore_probe_failed"
    assert exclude_bytes(repo) == repo_before and exclude_bytes(wrong) == wrong_before
    assert not (repo / ".codex" / "state").exists()
    assert not (wrong / ".codex" / "state").exists()


def test_non_repo_falls_back_to_supported_global_root(tmp_path: Path) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    location = resolve_state_location(
        ROOT, "claude-code/claude-code-cli", cwd=cwd, home=home
    )
    assert location.scope == "global"
    assert location.root == home / ".claude" / "state"
    assert location.state_path == "~/.claude/state"
    assert location.git is None


@pytest.mark.parametrize("marker_kind", ["directory", "missing-gitdir"])
def test_broken_git_marker_never_falls_back_to_global_state(
    tmp_path: Path, marker_kind: str
) -> None:
    cwd = tmp_path / "private-broken-repo"
    cwd.mkdir()
    marker = cwd / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: ../private-missing-gitdir\n", encoding="utf-8")
    home = tmp_path / "private-home"

    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="auto"
        )

    assert failure.value.code == "git_probe_failed"
    assert not (home / ".codex" / "state").exists()


@pytest.mark.parametrize("new_git_error", [False, True])
def test_git_marker_discovery_stops_before_cross_filesystem_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_git_error: bool
) -> None:
    foreign_parent = tmp_path / "foreign-filesystem"
    foreign_parent.mkdir()
    (foreign_parent / ".git").write_text(
        "gitdir: ../private-missing-gitdir\n", encoding="utf-8"
    )
    mount_root = foreign_parent / "mount-root"
    cwd = mount_root / "nested"
    cwd.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    original = Path.stat
    foreign_device = foreign_parent.stat().st_dev + 1

    def mounted(path: Path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return stat_with_dev(result, foreign_device) if path == foreign_parent else result

    monkeypatch.setattr(Path, "stat", mounted)
    if new_git_error:
        original_git = locator._git

        def newer_git(path: Path, *args: str):
            if args == ("rev-parse", "--is-inside-work-tree"):
                return subprocess.CompletedProcess(
                    ["git"], 128, "", "fatal: invalid gitfile format: foreign/.git")
            return original_git(path, *args)

        monkeypatch.setattr(locator, "_git", newer_git)
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="auto"
    )

    assert location.scope == "global"
    assert location.git is None
    assert not location.root.exists()


def test_invalid_gitfile_within_discovery_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "invalid"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: ../missing\n", encoding="utf-8")
    monkeypatch.setattr(locator, "_git", lambda *_args: subprocess.CompletedProcess(
        ["git"], 128, "", "fatal: invalid gitfile format: invalid/.git"))

    with pytest.raises(ClientStateError) as failure:
        probe_git_context(repo)
    assert failure.value.code == "git_probe_failed"


def test_git_marker_discovery_stat_uncertainty_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uncertain_parent = tmp_path / "private-uncertain-parent"
    cwd = uncertain_parent / "nested"
    cwd.mkdir(parents=True)
    home = tmp_path / "home"
    original = Path.stat

    def uncertain(path: Path, *args, **kwargs):
        if path == uncertain_parent:
            raise PermissionError("private mount-boundary detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", uncertain)
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="auto"
        )

    assert failure.value.code == "git_probe_failed"
    assert not (home / ".codex" / "state").exists()


def test_codex_home_override_can_be_outside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    codex_home = tmp_path / "client-data" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
    )
    assert location.root == codex_home / "state"
    assert location.owner_root == codex_home
    assert not location.root.exists()


def test_codex_home_unset_uses_home_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    monkeypatch.delenv("CODEX_HOME", raising=False)
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="global"
    )
    assert location.root == home / ".codex" / "state"
    assert location.owner_root == home / ".codex"
    assert not location.root.exists()


@pytest.mark.parametrize("surface", ["kwargs", "prepared"])
def test_artifact_content_exact_limit_allocates_and_verifies(
    tmp_path: Path, surface: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    options = artifact_options(b"x" * (16 * 1024 * 1024))
    if surface == "prepared":
        allocated = allocate_artifact(location, prepared=prepare_artifact_request(location, **options))
    else:
        allocated = allocate_artifact(location, **options)
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)["ok"]


@pytest.mark.parametrize("surface", ["kwargs", "prepared"])
@pytest.mark.parametrize("invalid", ["oversized", "nonbytes"])
def test_artifact_content_preflight_rejects_without_mutation(
    tmp_path: Path, surface: str, invalid: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    before = exclude_bytes(repo)
    content: object = b"x" * (16 * 1024 * 1024 + 1) if invalid == "oversized" else bytearray(b"x")
    with pytest.raises(ClientStateError) as failure:
        if surface == "prepared":
            prepared = prepare_artifact_request(location, **artifact_options())
            allocate_artifact(location, prepared=replace(prepared, content=content))
        else:
            allocate_artifact(location, **artifact_options(content))
    assert failure.value.code == "invalid_content"
    assert exclude_bytes(repo) == before
    assert not (repo / ".codex" / "state").exists()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("agent", 1, "invalid_agent"),
        ("purpose", "reserved-01", "invalid_purpose"),
        ("extension", "md.exe", "invalid_extension"),
        ("kind", "a" * 33, "invalid_kind"),
        ("schema", "Bad", "invalid_schema"),
        ("producer", "a" * 65, "invalid_producer"),
        ("predecessor", 1, "invalid_predecessor"),
        ("checkpoint", "../escape", "invalid_checkpoint"),
        ("consumers", ("beta", "alpha"), "invalid_consumer"),
        ("consumers", ("alpha", "alpha"), "invalid_consumer"),
        ("consumers", ["alpha"], "invalid_consumer"),
        ("created_at", "20260715T12000000Z", "invalid_artifact_name"),
        ("metadata_schema", [], "invalid_metadata_schema"),
    ],
)
def test_forged_prepared_request_is_revalidated_before_mutation(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    prepared = prepare_artifact_request(location, **artifact_options())
    before = exclude_bytes(repo)
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(location, prepared=replace(prepared, **{field: value}))
    assert failure.value.code == code
    assert exclude_bytes(repo) == before
    assert not (repo / ".codex" / "state").exists()


def test_custom_metadata_schema_can_tighten_but_not_weaken_official_baseline(
    tmp_path: Path
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    weak = tmp_path / "weak.json"
    weak.write_text("{}\n", encoding="utf-8")
    allocated = allocate_artifact(
        location, metadata_schema=weak, **artifact_options()
    )
    metadata_path = location.root / allocated["metadata"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["unexpected"] = True
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError) as weak_failure:
        verify_artifact(current, allocated["artifact"], schema_path=weak)
    assert weak_failure.value.code == "invalid_metadata"

    tighter = tmp_path / "tighter.json"
    tighter.write_text(
        json.dumps({"type": "object", "properties": {"producer": {"const": "required"}}}),
        encoding="utf-8",
    )
    clean = repository(tmp_path / "clean")
    clean_location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=clean, home=tmp_path / "home"
    )
    with pytest.raises(ClientStateError) as tight_failure:
        prepare_artifact_request(clean_location, metadata_schema=tighter, **artifact_options())
    assert tight_failure.value.code == "invalid_metadata"
    assert not (clean / ".codex" / "state").exists()


def test_prepare_rejects_string_consumers_before_mutation(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    before = exclude_bytes(repo)
    with pytest.raises(ClientStateError) as failure:
        prepare_artifact_request(location, consumers="codex", **artifact_options())
    assert failure.value.code == "invalid_consumer"
    assert exclude_bytes(repo) == before
    assert not (repo / ".codex" / "state").exists()


def test_verify_rejects_noncanonical_consumer_order(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, consumers=["beta", "alpha"], **artifact_options()
    )
    metadata_path = location.root / allocated["metadata"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["consumers"] == ["alpha", "beta"]
    metadata["consumers"] = ["beta", "alpha"]
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError) as failure:
        verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)
    assert failure.value.code == "invalid_metadata"


@pytest.mark.parametrize("value", ["", "   ", "relative-client-home"])
def test_codex_home_invalid_values_fail_without_cwd_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CODEX_HOME", value)
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
        )
    assert failure.value.code == "invalid_environment"
    assert not (cwd / "state").exists()


@pytest.mark.parametrize("scope", ["repo", "global"])
@pytest.mark.parametrize("position", ["owner", "root"])
@pytest.mark.parametrize("node_kind", ["file", "symlink"])
def test_state_path_component_collisions_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    position: str,
    node_kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if scope == "repo":
        cwd = repository(tmp_path / "repo")
        target = cwd / ".codex" if position == "owner" else cwd / ".codex" / "state"
        if position == "root":
            target.parent.mkdir()
        before = exclude_bytes(cwd)
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        owner = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(owner))
        target = owner if position == "owner" else owner / "state"
        if position == "root":
            owner.mkdir()
        before = None
    if node_kind == "file":
        target.write_bytes(b"foreign\n")
    else:
        outside = tmp_path / f"outside-{scope}-{position}"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope=scope
        )
    assert failure.value.code == "unsafe_state_path"
    if before is not None:
        assert exclude_bytes(cwd) == before
    assert target.is_symlink() if node_kind == "symlink" else target.read_bytes() == b"foreign\n"


def test_codex_home_ancestor_symlink_is_rejected_but_normal_external_home_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked / "codex"))
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
        )
    assert failure.value.code == "unsafe_state_path"
    assert not (outside / "codex").exists()

    external = tmp_path / "external" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(external))
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
    )
    assert location.owner_root == external and location.root == external / "state"


@pytest.mark.parametrize("node_kind", ["file", "symlink", "directory"])
def test_multicomponent_global_intermediate_uses_registered_opencode_shape(
    tmp_path: Path, node_kind: str
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    intermediate = home / ".config" / "opencode"
    intermediate.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    if node_kind == "file":
        intermediate.write_bytes(b"foreign\n")
    elif node_kind == "symlink":
        outside.mkdir()
        intermediate.symlink_to(outside, target_is_directory=True)
    else:
        intermediate.mkdir()

    if node_kind == "directory":
        location = resolve_state_location(
            ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
        )
        assert location.root == intermediate / "state" and not location.root.exists()
    else:
        with pytest.raises(ClientStateError) as failure:
            resolve_state_location(
                ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
            )
        assert failure.value.code == "unsafe_state_path"
        assert not (outside / "state").exists()
        assert intermediate.is_symlink() if node_kind == "symlink" else intermediate.read_bytes() == b"foreign\n"


@pytest.mark.parametrize("position", ["owner", "intermediate", "root", "pre-owner"])
def test_global_locator_rejects_foreign_owned_managed_chain_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    if position in {"owner", "pre-owner"}:
        container = tmp_path / "container"
        container.mkdir()
        owner = container / "codex"
        monkeypatch.setenv("CODEX_HOME", str(owner))
        client = "codex/codex-cli"
        if position == "owner":
            owner.mkdir()
            target = owner
        else:
            target = container
    else:
        state = home / ".config" / "opencode" / "state"
        state.mkdir(parents=True)
        owner = home
        client = "opencode/opencode-cli"
        target = state.parent if position == "intermediate" else state
    original = Path.stat
    foreign_uid = os.geteuid() + 1

    def foreign(path: Path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return stat_with_uid(result, foreign_uid) if path == target else result

    monkeypatch.setattr(Path, "stat", foreign)
    with pytest.raises(ClientStateError) as failure:
        resolve_state_location(ROOT, client, cwd=cwd, home=home, scope="global")
    assert failure.value.code == "unsafe_state_path"
    if position in {"owner", "pre-owner"}:
        assert not (owner / "state").exists()
    else:
        assert list(state.iterdir()) == []


@pytest.mark.parametrize("control", ["root-pre-owner", "system-ancestor", "repo-root"])
def test_ownership_controls_do_not_expand_global_or_repo_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    original = Path.stat
    if control == "repo-root":
        repo = repository(tmp_path / "repo")
        target = repo
        client = "codex/codex-cli"
        kwargs = {"cwd": repo, "home": tmp_path / "home", "scope": "repo"}
        replacement_uid = os.geteuid() + 1
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        container = tmp_path / "container"
        container.mkdir()
        owner = container / "codex"
        monkeypatch.setenv("CODEX_HOME", str(owner))
        client = "codex/codex-cli"
        kwargs = {"cwd": cwd, "home": tmp_path / "home", "scope": "global"}
        if control == "root-pre-owner":
            target = container
            replacement_uid = 0
        else:
            target = tmp_path.parent
            replacement_uid = os.geteuid() + 1

    def controlled(path: Path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return stat_with_uid(result, replacement_uid) if path == target else result

    monkeypatch.setattr(Path, "stat", controlled)
    location = resolve_state_location(ROOT, client, **kwargs)
    assert location.scope == ("repo" if control == "repo-root" else "global")


@pytest.mark.parametrize("control", ["repo-root", "unrelated-global-ancestor"])
def test_global_mode_policy_does_not_expand_to_repo_or_unrelated_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    home = tmp_path / "home"
    if control == "repo-root":
        cwd = repository(tmp_path / "repo")
        cwd.chmod(0o777)
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="repo"
        )
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        unrelated.chmod(0o777)
        immediate_parent = unrelated / "safe-parent"
        immediate_parent.mkdir()
        owner = immediate_parent / "codex-home"
        owner.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(owner))
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="global"
        )

    allocated = allocate_artifact(location, **artifact_options())
    assert (location.root / allocated["artifact"]).is_file()


@pytest.mark.parametrize(("mode", "safe"), [(0o1777, True), (0o777, False)])
def test_existing_global_owner_immediate_parent_mode_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, safe: bool
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    immediate_parent = tmp_path / "unsafe-parent"
    immediate_parent.mkdir()
    immediate_parent.chmod(mode)
    owner = immediate_parent / "codex-home"
    owner.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(owner))

    if safe:
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
        )
        allocated = allocate_artifact(location, **artifact_options())
        assert (owner / "state" / allocated["artifact"]).is_file()
    else:
        with pytest.raises(ClientStateError) as failure:
            resolve_state_location(
                ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
            )
        assert failure.value.code == "unsafe_state_path"
        assert not (owner / "state").exists()


@pytest.mark.parametrize("position", ["owner", "intermediate", "root"])
def test_global_allocator_rechecks_live_descriptor_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    state = home / ".config" / "opencode" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(
        ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
    )
    targets = {"owner": home, "intermediate": state.parent, "root": state}
    target_identity = (targets[position].stat().st_dev, targets[position].stat().st_ino)
    real_fstat = os.fstat

    def foreign_descriptor(fd: int):
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == target_identity:
            return stat_with_uid(result, os.geteuid() + 1)
        return result

    monkeypatch.setattr(artifacts.os, "fstat", foreign_descriptor)
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(location, **artifact_options())
    assert failure.value.code == "unsafe_state_path"
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("operation", ["allocate", "verify"])
@pytest.mark.parametrize("position", ["owner", "intermediate", "root"])
def test_global_operation_rechecks_live_descriptor_permissions_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    state = home / ".config" / "opencode" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(
        ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
    )
    artifact = None
    if operation == "verify":
        artifact = allocate_artifact(location, **artifact_options())["artifact"]
        location = resolve_state_location(
            ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
        )
    before = {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}
    targets = {"owner": home, "intermediate": state.parent, "root": state}
    target_details = targets[position].stat()
    target_identity = (target_details.st_dev, target_details.st_ino)
    real_fstat = os.fstat

    def unsafe_permissions(fd: int):
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == target_identity:
            return stat_with_mode(result, 0o770)
        return result

    monkeypatch.setattr(artifacts.os, "fstat", unsafe_permissions)
    with pytest.raises(ClientStateError) as failure:
        if operation == "allocate":
            allocate_artifact(location, **artifact_options())
        else:
            assert artifact is not None
            verify_artifact(location, artifact, schema_path=SCHEMA)
    assert failure.value.code == "unsafe_state_path"
    assert {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("position", ["owner", "intermediate", "root"])
@pytest.mark.parametrize(
    ("mode", "safe"),
    [
        (0o700, True), (0o755, True), (0o2755, True),
        (0o770, False), (0o777, False), (0o1777, False), (0o2770, False),
    ],
)
def test_global_managed_directory_mode_policy(
    tmp_path: Path, position: str, mode: int, safe: bool
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    home = tmp_path / "home"
    state = home / ".config" / "opencode" / "state"
    state.mkdir(parents=True)
    targets = {"owner": home, "intermediate": state.parent, "root": state}
    targets[position].chmod(mode)
    if safe:
        location = resolve_state_location(
            ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
        )
        assert location.root == state
    else:
        with pytest.raises(ClientStateError) as failure:
            resolve_state_location(
                ROOT, "opencode/opencode-cli", cwd=cwd, home=home, scope="global"
            )
        assert failure.value.code == "unsafe_state_path"
        assert list(state.iterdir()) == []


@pytest.mark.parametrize(("mode", "safe"), [(0o755, True), (0o1777, True), (0o777, False)])
def test_global_missing_owner_predecessor_mode_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, safe: bool
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    boundary.chmod(mode)
    owner = boundary / "missing" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(owner))
    if safe:
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
        )
        assert location.owner_root == owner and not owner.exists()
    else:
        with pytest.raises(ClientStateError) as failure:
            resolve_state_location(
                ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
            )
        assert failure.value.code == "unsafe_state_path"
        assert list(boundary.iterdir()) == []


@pytest.mark.parametrize("operation", ["allocate", "verify"])
def test_global_pre_owner_descriptor_uid_change_after_resolution_fails_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    repo = repository(tmp_path / "repo")
    before = exclude_bytes(repo)
    container = tmp_path / "private-container"
    container.mkdir()
    owner = container / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(owner))
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home", scope="global"
    )
    target_details = container.stat()
    target_identity = (target_details.st_dev, target_details.st_ino)
    real_fstat = os.fstat

    def foreign_descriptor(fd: int):
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == target_identity:
            return stat_with_uid(result, os.geteuid() + 1)
        return result

    monkeypatch.setattr(artifacts.os, "fstat", foreign_descriptor)
    with pytest.raises(ClientStateError) as failure:
        if operation == "allocate":
            allocate_artifact(location, **artifact_options())
        else:
            verify_artifact(
                location,
                "codex-cli-20260715T120000000Z-w18-probe.md",
                schema_path=SCHEMA,
            )
    assert failure.value.code == "unsafe_state_path"
    assert exclude_bytes(repo) == before
    assert not owner.exists()
    assert list(container.iterdir()) == []


@pytest.mark.parametrize(("mode", "safe"), [(0o755, True), (0o1777, True), (0o777, False)])
def test_global_pre_owner_live_descriptor_permission_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, safe: bool
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    owner = boundary / "missing" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(owner))
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
    )
    boundary_details = boundary.stat()
    identity = (boundary_details.st_dev, boundary_details.st_ino)
    real_fstat = os.fstat

    def selected_mode(fd: int):
        result = real_fstat(fd)
        return stat_with_mode(result, mode) if (result.st_dev, result.st_ino) == identity else result

    monkeypatch.setattr(artifacts.os, "fstat", selected_mode)
    if safe:
        allocated = allocate_artifact(location, **artifact_options())
        assert (location.root / allocated["artifact"]).is_file()
    else:
        with pytest.raises(ClientStateError) as failure:
            allocate_artifact(location, **artifact_options())
        assert failure.value.code == "unsafe_state_path"
        assert list(boundary.iterdir()) == []


def test_global_live_traversal_rejects_new_unsafe_pre_owner_intermediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    owner = boundary / "appears-later" / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(owner))
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=cwd, home=tmp_path / "home", scope="global"
    )
    original = artifacts._open_absolute_directory

    def create_intermediate_then_open(path: Path, **kwargs):
        intermediate = boundary / "appears-later"
        intermediate.mkdir()
        intermediate.chmod(0o777)
        return original(path, **kwargs)

    monkeypatch.setattr(artifacts, "_open_absolute_directory", create_intermediate_then_open)
    before_fds = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(location, **artifact_options())
    assert failure.value.code == "unsafe_state_path"
    assert descriptor_count() == before_fds
    assert not owner.exists()
    assert list((boundary / "appears-later").iterdir()) == []


@pytest.mark.parametrize(
    "control", ["boundary-root", "boundary-euid", "foreign-unrelated", "repo-root"]
)
def test_live_pre_owner_descriptor_check_preserves_ownership_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    home = tmp_path / "home"
    if control == "repo-root":
        cwd = repository(tmp_path / "repo")
        location = resolve_state_location(ROOT, "codex/codex-cli", cwd=cwd, home=home)
        target = cwd
        replacement_uid = os.geteuid() + 1
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        container = tmp_path / "container"
        container.mkdir()
        owner = container / "codex"
        monkeypatch.setenv("CODEX_HOME", str(owner))
        if control == "foreign-unrelated":
            owner.mkdir()
            target = tmp_path.parent
            replacement_uid = os.geteuid() + 1
        else:
            target = container
            replacement_uid = 0 if control == "boundary-root" else os.geteuid()
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=cwd, home=home, scope="global"
        )
    target_details = target.stat()
    target_identity = (target_details.st_dev, target_details.st_ino)
    real_fstat = os.fstat

    def controlled_descriptor(fd: int):
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == target_identity:
            return stat_with_uid(result, replacement_uid)
        return result

    monkeypatch.setattr(artifacts.os, "fstat", controlled_descriptor)
    allocated = allocate_artifact(location, **artifact_options())
    assert (location.root / allocated["artifact"]).is_file()


@pytest.mark.parametrize("operation", ["allocate", "verify"])
def test_state_parent_symlink_introduced_after_resolution_fails_refresh_without_foreign_write(
    tmp_path: Path, operation: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    artifact = None
    if operation == "verify":
        artifact = allocate_artifact(location, **artifact_options())["artifact"]
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
        )
    owner = repo / ".codex"
    if owner.exists():
        owner.rename(repo / ".codex-prior")
    outside = tmp_path / "outside"
    outside.mkdir()
    owner.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ClientStateError) as failure:
        if operation == "allocate":
            allocate_artifact(location, **artifact_options())
        else:
            assert artifact is not None
            verify_artifact(location, artifact, schema_path=SCHEMA)
    assert failure.value.code == "unsafe_state_path"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "file", "permission"])
def test_probe_directory_failures_are_typed_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target = tmp_path / "private-target"
    if kind == "file":
        target.write_text("not a directory\n", encoding="utf-8")
    elif kind == "permission":
        original_resolve = Path.resolve

        def deny_resolve(path: Path, *args, **kwargs):
            if path == target:
                raise PermissionError("private permission detail")
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", deny_resolve)
    with pytest.raises(ClientStateError) as failure:
        probe_git_context(target)
    assert failure.value.code == "invalid_directory"
    assert str(target) not in str(failure.value)


def test_bare_repository_and_ambiguous_probe_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    with pytest.raises(ClientStateError, match="bare repositories"):
        probe_git_context(bare)

    failed = subprocess.CompletedProcess(["git"], 2, "", "permission denied")
    monkeypatch.setattr(locator, "_git", lambda *_args: failed)
    with pytest.raises(ClientStateError, match="ambiguous Git"):
        probe_git_context(tmp_path)


def test_unsupported_unknown_and_symlink_states_fail_closed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    unsupported_repo = repository(tmp_path / "unsupported-repo")
    with pytest.raises(ClientStateError, match="no supported repo"):
        resolve_state_location(
            ROOT, "antigravity/antigravity-ide", cwd=unsupported_repo,
            home=tmp_path / "home", scope="repo",
        )
    with pytest.raises(ClientStateError, match="unknown or invalid"):
        resolve_state_location(ROOT, "missing/missing-cli", cwd=plain, home=tmp_path / "home")

    repo = repository(tmp_path / "repo")
    (repo / ".codex").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ClientStateError, match="symlink"):
        resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")


def test_exact_git_exclude_plan_apply_and_concurrent_change(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert plan.action == "append"
    assert plan.entry == "/.codex/state/"
    applied = apply_git_exclude(plan)
    assert applied.action == "applied"
    assert location.git
    text = location.git.exclude_path.read_text(encoding="utf-8")
    assert text.endswith("/.codex/state/\n")
    assert plan_git_exclude(location).action == "already-ignored"

    other = resolve_state_location(ROOT, "claude-code/claude-code-cli", cwd=repo, home=tmp_path / "home")
    stale = plan_git_exclude(other)
    location.git.exclude_path.write_text(text + "/other/\n", encoding="utf-8")
    apply_git_exclude(stale)
    merged = location.git.exclude_path.read_text(encoding="utf-8")
    assert "/other/\n" in merged and "/.claude/state/\n" in merged


@pytest.mark.parametrize(
    "missing",
    [
        "repo_root_device", "repo_root_inode", "exclude_parent_device",
        "exclude_parent_inode", "exclude_present", "exclude_device", "exclude_inode",
    ],
)
@pytest.mark.parametrize("initially_ignored", [False, True])
def test_git_exclude_plan_carries_private_identity_tokens_and_rejects_missing_token(
    tmp_path: Path, missing: str, initially_ignored: bool
) -> None:
    repo = repository(tmp_path / "repo")
    if initially_ignored:
        (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    expected_action = "already-ignored" if initially_ignored else "append"
    assert plan.payload() == {"action": expected_action, "entry": "/.codex/state/"}
    assert "repo_root_device" not in repr(plan)
    assert all(
        getattr(plan, field) is not None
        for field in (
            "repo_root_device", "repo_root_inode", "exclude_parent_device",
            "exclude_parent_inode", "exclude_present", "exclude_device", "exclude_inode",
        )
    )
    before = exclude_bytes(repo)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(replace(plan, **{missing: None}))
    assert failure.value.code == "stale_state_binding"
    assert exclude_bytes(repo) == before
    assert not location.git.exclude_path.with_name("exclude.localsetup.lock").exists()


@pytest.mark.parametrize(("target", "mode"), [("exclude", 0o660), ("exclude", 0o666), ("lock", 0o660), ("lock", 0o666)])
def test_git_exclude_rejects_group_or_other_writable_mutable_files(
    tmp_path: Path, target: str, mode: int
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    if target == "exclude":
        candidate = location.git.exclude_path
        candidate.chmod(mode)
        operation = lambda: plan_git_exclude(location)
    else:
        plan = plan_git_exclude(location)
        candidate = location.git.exclude_path.with_name("exclude.localsetup.lock")
        candidate.write_bytes(b"")
        candidate.chmod(mode)
        operation = lambda: apply_git_exclude(plan)
    before = candidate.read_bytes()
    with pytest.raises(ClientStateError) as failure:
        operation()
    assert failure.value.code == "unsafe_exclude"
    assert candidate.read_bytes() == before


@pytest.mark.parametrize("mode", [0o600, 0o644])
@pytest.mark.parametrize("target", ["exclude", "lock"])
def test_git_exclude_accepts_safe_mutable_file_modes(
    tmp_path: Path, target: str, mode: int
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    if target == "exclude":
        candidate = location.git.exclude_path
        candidate.chmod(mode)
        plan = plan_git_exclude(location)
    else:
        plan = plan_git_exclude(location)
        candidate = location.git.exclude_path.with_name("exclude.localsetup.lock")
        candidate.write_bytes(b"")
        candidate.chmod(mode)
    applied = apply_git_exclude(plan)
    assert applied.action == "applied"
    assert applied.repo_root_device == plan.repo_root_device
    assert applied.exclude_parent_inode == plan.exclude_parent_inode


@pytest.mark.parametrize("replacement", ["repo", "git", "info", "exclude"])
def test_git_exclude_rejects_same_path_identity_replacement_before_lock_or_mutation(
    tmp_path: Path, replacement: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    if replacement == "repo":
        repo.rename(tmp_path / "repo-prior")
        replacement_repo = repository(repo)
        active_exclude = replacement_repo / ".git" / "info" / "exclude"
    elif replacement == "git":
        (repo / ".git").rename(repo / ".git-prior")
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        active_exclude = repo / ".git" / "info" / "exclude"
    elif replacement == "info":
        info = exclude.parent
        info.rename(info.with_name("info-prior"))
        info.mkdir()
        active_exclude = info / "exclude"
        active_exclude.write_bytes(before)
    else:
        replacement_file = tmp_path / "replacement-exclude"
        replacement_file.write_bytes(before)
        os.replace(replacement_file, exclude)
        active_exclude = exclude
    active_before = active_exclude.read_bytes()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "stale_state_binding"
    assert active_exclude.read_bytes() == active_before
    assert not active_exclude.with_name("exclude.localsetup.lock").exists()
    assert not (repo / ".codex" / "state").exists()


def test_git_exclude_expected_absent_uses_exclusive_creation_and_never_falls_back(
    tmp_path: Path
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    assert plan.exclude_present is False
    exclude.write_bytes(b"foreign\n")
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "stale_state_binding"
    assert exclude.read_bytes() == b"foreign\n"
    assert not exclude.with_name("exclude.localsetup.lock").exists()

    clean = repository(tmp_path / "clean")
    clean_location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=clean, home=tmp_path / "home"
    )
    assert clean_location.git
    clean_exclude = clean_location.git.exclude_path
    clean_exclude.unlink()
    clean_plan = plan_git_exclude(clean_location)
    applied = apply_git_exclude(clean_plan)
    assert clean_plan.exclude_present is False and applied.action == "applied"
    assert clean_exclude.read_bytes() == b"/.codex/state/\n"


def test_git_exclude_created_absent_is_restored_on_prewrite_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    baseline = descriptor_count()

    def fail_probe(_root: Path, _entry: str) -> bool:
        raise ClientStateError("injected ignore probe failure", code="git_ignore_probe_failed")

    monkeypatch.setattr(git_exclude, "_effective_ignore", fail_probe)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "git_ignore_probe_failed"
    assert not exclude.exists()
    assert descriptor_count() == baseline


def test_git_exclude_created_absent_is_restored_when_concurrently_ignored(
    tmp_path: Path
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
    applied = apply_git_exclude(plan)
    assert applied.action == "already-ignored"
    assert applied.exclude_present is False and applied.exclude_device is None
    assert applied.payload() == {"action": "already-ignored", "entry": "/.codex/state/"}
    assert not exclude.exists()


def test_git_exclude_created_ineffective_postwrite_preserves_current_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    monkeypatch.setattr(git_exclude, "_effective_ignore", lambda *_args: False)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == b"/.codex/state/\n"


def test_git_exclude_created_cleanup_preserves_replacement_and_reports_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    probes = 0

    def replace_then_fail(_root: Path, _entry: str) -> bool:
        nonlocal probes
        probes += 1
        if probes == 1:
            return False
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replacement\n")
        os.replace(replacement, exclude)
        raise ClientStateError("injected ignore probe failure", code="git_ignore_probe_failed")

    monkeypatch.setattr(git_exclude, "_effective_ignore", replace_then_fail)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == b"replacement\n"


def test_git_exclude_created_parent_fsync_failure_preserves_current_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    parent_identity = (exclude.parent.stat().st_dev, exclude.parent.stat().st_ino)
    real_fsync = os.fsync

    def fail_parent_fsync(fd: int) -> None:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == parent_identity:
            raise OSError("injected parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(git_exclude.os, "fsync", fail_parent_fsync)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == b"/.codex/state/\n"


def test_git_exclude_new_creation_fsyncs_file_before_parent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    parent_identity = (exclude.parent.stat().st_dev, exclude.parent.stat().st_ino)
    real_fsync = os.fsync
    order: list[str] = []

    def record_fsync(fd: int) -> None:
        details = os.fstat(fd)
        order.append("parent" if (details.st_dev, details.st_ino) == parent_identity else "file")
        real_fsync(fd)

    monkeypatch.setattr(git_exclude.os, "fsync", record_fsync)
    assert apply_git_exclude(plan).action == "applied"
    assert order[-2:] == ["file", "parent"]


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_git_exclude_planned_present_unsafe_swap_is_typed_and_closes_descriptors(
    tmp_path: Path, replacement: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    exclude = location.git.exclude_path
    if replacement == "symlink":
        candidate = tmp_path / "replacement-symlink"
        candidate.symlink_to(tmp_path / "foreign")
    else:
        candidate = tmp_path / "replacement-directory"
        candidate.mkdir()
    exclude.unlink()
    os.replace(candidate, exclude)
    assert (exclude.lstat().st_dev, exclude.lstat().st_ino) != (
        plan.exclude_device, plan.exclude_inode
    )
    baseline = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "stale_state_binding"
    assert descriptor_count() == baseline


@pytest.mark.parametrize("target", ["lock", "exclude"])
def test_git_exclude_post_open_fstat_failure_is_typed_and_descriptor_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    real_fstat = os.fstat
    injected = False

    def fail_target(fd: int):
        nonlocal injected
        link = descriptor_target(fd)
        selected = link.endswith("exclude.localsetup.lock") if target == "lock" else link.endswith("/exclude")
        if selected and not injected:
            injected = True
            raise OSError("injected fstat failure")
        return real_fstat(fd)

    baseline = descriptor_count()
    monkeypatch.setattr(git_exclude.os, "fstat", fail_target)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "unsafe_exclude"
    assert injected and descriptor_count() == baseline


@pytest.mark.parametrize("failure_point", ["stat", "open", "read"])
def test_git_exclude_present_io_errors_are_typed_and_descriptor_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    plan = plan_git_exclude(location)
    before = exclude.read_bytes()
    injected = False
    if failure_point == "stat":
        real_stat = os.stat

        def fail_exclude_stat(path, *args, **kwargs):
            nonlocal injected
            if path == exclude.name and kwargs.get("dir_fd") is not None and not injected:
                injected = True
                raise PermissionError("injected exclude stat failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(git_exclude.os, "stat", fail_exclude_stat)
    elif failure_point == "open":
        real_open = os.open

        def fail_exclude_open(path, flags, *args, **kwargs):
            nonlocal injected
            if path == exclude.name and not flags & os.O_EXCL and not injected:
                injected = True
                raise OSError("injected exclude open failure")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(git_exclude.os, "open", fail_exclude_open)
    else:
        real_read = git_exclude._read_fd

        def fail_exclude_read(fd: int) -> bytes:
            nonlocal injected
            if descriptor_target(fd).endswith("/exclude") and not injected:
                injected = True
                raise OSError("injected exclude read failure")
            return real_read(fd)

        monkeypatch.setattr(git_exclude, "_read_fd", fail_exclude_read)
    baseline = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "unsafe_exclude"
    assert injected and descriptor_count() == baseline
    assert exclude.read_bytes() == before


def test_git_exclude_created_read_failure_preserves_empty_file_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    real_read = git_exclude._read_fd
    injected = False

    def fail_first_exclude_read(fd: int) -> bytes:
        nonlocal injected
        if descriptor_target(fd).endswith("/exclude") and not injected:
            injected = True
            raise OSError("injected exclude read failure")
        return real_read(fd)

    baseline = descriptor_count()
    monkeypatch.setattr(git_exclude, "_read_fd", fail_first_exclude_read)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert injected and descriptor_count() == baseline
    assert exclude.read_bytes() == b""


@pytest.mark.parametrize("phase", ["postcreate-prewrite", "postwrite"])
def test_git_exclude_created_foreign_bytes_are_preserved_and_always_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    foreign = b"/foreign-before-local/\n"
    if phase == "postcreate-prewrite":
        real_read = git_exclude._read_fd
        injected = False

        def inject_foreign_then_fail_read(fd: int) -> bytes:
            nonlocal injected
            if descriptor_target(fd).endswith("/exclude") and not injected:
                injected = True
                with exclude.open("ab") as handle:
                    handle.write(foreign)
                raise OSError("injected post-creation read failure")
            return real_read(fd)

        monkeypatch.setattr(git_exclude, "_read_fd", inject_foreign_then_fail_read)
        monkeypatch.setattr(git_exclude, "_effective_ignore", lambda *_args: False)
    else:
        probes = 0

        def inject_foreign_then_fail_probe(_root: Path, _entry: str) -> bool:
            nonlocal probes
            probes += 1
            if probes == 2:
                with exclude.open("ab") as handle:
                    handle.write(foreign)
            return False

        monkeypatch.setattr(git_exclude, "_effective_ignore", inject_foreign_then_fail_probe)
    baseline = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    expected = foreign if phase == "postcreate-prewrite" else b"/.codex/state/\n" + foreign
    assert exclude.read_bytes() == expected
    assert descriptor_count() == baseline


def test_git_exclude_existing_already_ignored_digest_uses_final_bound_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    plan = plan_git_exclude(location)
    real_effective_ignore = git_exclude._effective_ignore
    foreign_rule = b"/.codex/state/\n"

    def append_rule_during_probe(root: Path, entry: str) -> bool:
        with exclude.open("ab") as handle:
            handle.write(foreign_rule)
        return real_effective_ignore(root, entry)

    monkeypatch.setattr(git_exclude, "_effective_ignore", append_rule_during_probe)
    applied = apply_git_exclude(plan)
    final = exclude.read_bytes()
    assert applied.action == "already-ignored"
    assert final.endswith(foreign_rule)
    assert applied.expected_digest == hashlib.sha256(final).hexdigest()


def test_git_exclude_plan_final_real_probe_read_rejects_foreign_invalid_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    real_effective_ignore = git_exclude._effective_ignore

    def real_probe_then_invalidate(root: Path, entry: str) -> bool:
        result = real_effective_ignore(root, entry)
        with exclude.open("ab") as handle:
            handle.write(b"\xff")
        return result

    baseline = descriptor_count()
    monkeypatch.setattr(git_exclude, "_effective_ignore", real_probe_then_invalidate)
    with pytest.raises(ClientStateError) as failure:
        plan_git_exclude(location)
    assert failure.value.code == "unsafe_exclude"
    assert exclude.read_bytes() == before + b"\xff"
    assert descriptor_count() == baseline


def test_git_exclude_existing_already_ignored_final_read_rejects_invalid_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    plan = plan_git_exclude(location)
    real_effective_ignore = git_exclude._effective_ignore
    foreign = b"/.codex/state/\n\xff"

    def establish_ignore_then_invalidate(root: Path, entry: str) -> bool:
        with exclude.open("ab") as handle:
            handle.write(b"/.codex/state/\n")
        result = real_effective_ignore(root, entry)
        with exclude.open("ab") as handle:
            handle.write(b"\xff")
        return result

    monkeypatch.setattr(git_exclude, "_effective_ignore", establish_ignore_then_invalidate)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "unsafe_exclude"
    assert exclude.read_bytes() == before + foreign


def test_git_exclude_existing_applied_invalid_utf8_preserves_current_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    plan = plan_git_exclude(location)
    real_effective_ignore = git_exclude._effective_ignore
    probes = 0

    def invalidate_before_local_append(root: Path, entry: str) -> bool:
        nonlocal probes
        probes += 1
        result = real_effective_ignore(root, entry)
        if probes == 1:
            with exclude.open("ab") as handle:
                handle.write(b"\xff\n")
        return result

    monkeypatch.setattr(git_exclude, "_effective_ignore", invalidate_before_local_append)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == before + b"\xff\n/.codex/state/\n"


def test_git_exclude_created_invalid_utf8_preserves_current_bytes_and_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)
    real_effective_ignore = git_exclude._effective_ignore
    probes = 0

    def inject_invalid(root: Path, entry: str) -> bool:
        nonlocal probes
        probes += 1
        result = real_effective_ignore(root, entry)
        if probes == 2:
            with exclude.open("ab") as handle:
                handle.write(b"\xff\n")
        return result

    monkeypatch.setattr(git_exclude, "_effective_ignore", inject_invalid)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == b"/.codex/state/\n\xff\n"


@pytest.mark.parametrize("planned_present", [False, True])
def test_git_exclude_lost_lock_postwrite_preserves_owned_and_contender_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned_present: bool
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not planned_present:
        exclude.unlink()
    before = exclude.read_bytes() if planned_present else b""
    plan = plan_git_exclude(location)
    lock = exclude.with_name("exclude.localsetup.lock")
    owned = b"/.codex/state/\n"
    contender = b"/contender/\n"
    probes = 0
    injected = False

    def replace_lock_and_append_during_postwrite_probe(_root: Path, _entry: str) -> bool:
        nonlocal probes
        nonlocal injected
        probes += 1
        if probes == 2 and not injected:
            injected = True
            replacement = tmp_path / "replacement-lock"
            replacement.write_bytes(b"replacement\n")
            os.replace(replacement, lock)
            with exclude.open("ab") as handle:
                handle.write(contender)
        return False

    baseline = descriptor_count()
    monkeypatch.setattr(
        git_exclude, "_effective_ignore", replace_lock_and_append_during_postwrite_probe
    )
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert injected and exclude.read_bytes() == before + owned + contender
    assert lock.read_bytes() == b"replacement\n"
    assert descriptor_count() == baseline


@pytest.mark.parametrize("probe_result", ["error", "already-ignored"])
def test_git_exclude_planned_absent_probe_finishes_before_exclusive_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_result: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    plan = plan_git_exclude(location)

    def controlled_probe(_root: Path, _entry: str) -> bool:
        if probe_result == "error":
            raise ClientStateError("ignore probe failed", code="git_ignore_probe_failed")
        return True

    def creation_must_not_run(*_args, **_kwargs):
        raise AssertionError("planned-absent probe must precede creation")

    monkeypatch.setattr(git_exclude, "_effective_ignore", controlled_probe)
    monkeypatch.setattr(git_exclude, "_create_mutable_regular_exclusive", creation_must_not_run)
    if probe_result == "error":
        with pytest.raises(ClientStateError) as failure:
            apply_git_exclude(plan)
        assert failure.value.code == "git_ignore_probe_failed"
    else:
        applied = apply_git_exclude(plan)
        assert applied.action == "already-ignored"
        assert applied.exclude_present is False
        assert applied.expected_digest == hashlib.sha256(b"").hexdigest()
    assert not exclude.exists()


@pytest.mark.parametrize("planned_present", [False, True])
def test_git_exclude_postwrite_failure_never_truncates_or_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned_present: bool
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not planned_present:
        exclude.unlink()
    before = exclude.read_bytes() if planned_present else b""
    plan = plan_git_exclude(location)

    def destructive_recovery_forbidden(*_args, **_kwargs):
        raise AssertionError("destructive Git exclude recovery is forbidden")

    monkeypatch.setattr(git_exclude.os, "ftruncate", destructive_recovery_forbidden)
    monkeypatch.setattr(git_exclude.os, "unlink", destructive_recovery_forbidden)
    monkeypatch.setattr(git_exclude, "_effective_ignore", lambda *_args: False)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == before + b"/.codex/state/\n"


@pytest.mark.parametrize("partial", [1, 8])
def test_git_exclude_partial_write_preserves_partial_and_noncooperating_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: int
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    plan = plan_git_exclude(location)
    real_write = os.write
    contender = b"/contender/\n"

    def partial_then_contender(fd: int, data: bytes) -> int:
        if b"/.codex/state/" not in data:
            return real_write(fd, data)
        written = real_write(fd, data[:partial])
        with exclude.open("ab") as handle:
            handle.write(contender)
        return written

    monkeypatch.setattr(git_exclude.os, "write", partial_then_contender)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert exclude.read_bytes() == before + b"/.codex/state/\n"[:partial] + contender


@pytest.mark.parametrize("action", ["append", "already-ignored"])
def test_git_exclude_plan_digest_tracks_final_valid_concurrent_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    real_effective_ignore = git_exclude._effective_ignore
    foreign = b"/foreign-during-plan/\n"

    def real_probe_then_append(root: Path, entry: str) -> bool:
        if action == "already-ignored":
            (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
        result = real_effective_ignore(root, entry)
        with exclude.open("ab") as handle:
            handle.write(foreign)
        return result

    monkeypatch.setattr(git_exclude, "_effective_ignore", real_probe_then_append)
    plan = plan_git_exclude(location)
    final = exclude.read_bytes()
    assert plan.action == action
    assert plan.expected_digest == hashlib.sha256(final).hexdigest()


@pytest.mark.parametrize("failure_point", ["fchmod", "flock", "contention", "absent-open"])
def test_git_exclude_precommit_os_errors_are_typed_without_descriptor_or_exclude_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if failure_point == "absent-open":
        exclude.unlink()
    plan = plan_git_exclude(location)
    before = exclude.read_bytes() if exclude.exists() else None
    if failure_point == "fchmod":
        real_fchmod = os.fchmod

        def fail_lock_fchmod(fd: int, mode: int) -> None:
            if descriptor_target(fd).endswith("exclude.localsetup.lock"):
                raise OSError("injected lock fchmod failure")
            real_fchmod(fd, mode)

        monkeypatch.setattr(git_exclude.os, "fchmod", fail_lock_fchmod)
    elif failure_point in {"flock", "contention"}:
        error = BlockingIOError("injected lock contention") if failure_point == "contention" else OSError("injected flock failure")
        monkeypatch.setattr(
            git_exclude.fcntl, "flock", lambda *_args: (_ for _ in ()).throw(error)
        )
    else:
        real_open = os.open

        def fail_exclusive_open(path, flags, *args, **kwargs):
            if path == exclude.name and flags & os.O_EXCL:
                raise PermissionError("injected exclusive open failure")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(git_exclude.os, "open", fail_exclusive_open)
    baseline = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    expected = "exclude_locked" if failure_point == "contention" else "unsafe_exclude"
    assert failure.value.code == expected
    assert descriptor_count() == baseline
    assert (exclude.read_bytes() if exclude.exists() else None) == before


@pytest.mark.parametrize("phase", ["prewrite", "postwrite"])
@pytest.mark.parametrize("target", ["lock", "exclude"])
def test_git_exclude_entry_replacement_is_detected_without_postwrite_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, target: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    exclude = location.git.exclude_path
    before = exclude.read_bytes()
    lock = exclude.with_name("exclude.localsetup.lock")
    lock.write_bytes(b"owned\n")
    candidate = lock if target == "lock" else exclude

    def replace_candidate() -> None:
        replacement = tmp_path / f"replacement-{target}"
        replacement.write_bytes(b"replacement\n")
        os.replace(replacement, candidate)

    if phase == "prewrite":
        real_flock = fcntl.flock

        def replace_after_flock(fd: int, operation: int) -> None:
            real_flock(fd, operation)
            replace_candidate()

        monkeypatch.setattr(git_exclude.fcntl, "flock", replace_after_flock)
    else:
        real_write = os.write

        def replace_after_write(fd: int, data: bytes) -> int:
            written = real_write(fd, data)
            if b"/.codex/state/" in data:
                replace_candidate()
            return written

        monkeypatch.setattr(git_exclude.os, "write", replace_after_write)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    expected_code = "exclude_commit_ambiguous" if phase == "postwrite" else "stale_state_binding"
    assert failure.value.code == expected_code
    assert candidate.read_bytes() == b"replacement\n"
    if target == "lock":
        expected_exclude = before + b"/.codex/state/\n" if phase == "postwrite" else before
        assert exclude.read_bytes() == expected_exclude


def test_existing_repo_ignore_needs_no_local_exclude_write(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert plan.action == "already-ignored"
    assert apply_git_exclude(plan) == plan


@pytest.mark.parametrize("coverage", ["external-exact", "external-broader", "local-exact"])
@pytest.mark.parametrize("outcome", ["stable", "lost"])
def test_already_ignored_plan_rechecks_live_coverage_and_preserves_current_bytes(
    tmp_path: Path, coverage: str, outcome: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    assert location.git
    exclude = location.git.exclude_path
    external = repo / ".gitignore"
    if coverage == "external-exact":
        external.write_bytes(b"/.codex/state/\n")
    elif coverage == "external-broader":
        external.write_bytes(b"/.codex/\n")
    else:
        exclude.write_bytes(exclude.read_bytes() + b"/.codex/state/\n")
    plan = plan_git_exclude(location)
    assert plan.action == "already-ignored"

    if coverage.startswith("external"):
        selected = external
        replacement = b"/foreign-external/\n"
    else:
        selected = exclude
        replacement = b"/foreign-local/\n"
    identity = (selected.stat().st_dev, selected.stat().st_ino)
    if outcome == "lost":
        selected.write_bytes(replacement)
        assert (selected.stat().st_dev, selected.stat().st_ino) == identity
    before = exclude.read_bytes()
    applied = apply_git_exclude(plan)
    final = exclude.read_bytes()

    if outcome == "stable":
        assert applied.action == "already-ignored"
        assert final == before
    else:
        assert applied.action == "applied"
        assert final == before + b"/.codex/state/\n"
    assert applied.expected_digest == hashlib.sha256(final).hexdigest()


def test_already_ignored_planned_absent_creates_exact_rule_when_external_coverage_is_lost(
    tmp_path: Path
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    assert location.git
    exclude = location.git.exclude_path
    exclude.unlink()
    external = repo / ".gitignore"
    external.write_bytes(b"/.codex/state/\n")
    plan = plan_git_exclude(location)
    assert plan.action == "already-ignored" and plan.exclude_present is False
    identity = (external.stat().st_dev, external.stat().st_ino)
    external.write_bytes(b"/foreign-external/\n")
    assert (external.stat().st_dev, external.stat().st_ino) == identity

    applied = apply_git_exclude(plan)

    assert applied.action == "applied"
    assert exclude.read_bytes() == b"/.codex/state/\n"
    assert applied.expected_digest == hashlib.sha256(exclude.read_bytes()).hexdigest()


@pytest.mark.parametrize("replacement", ["regular", "symlink", "directory", "missing"])
def test_already_ignored_planned_present_replacement_is_stale_before_lock(
    tmp_path: Path, replacement: str
) -> None:
    repo = repository(tmp_path / "repo")
    (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    assert location.git
    exclude = location.git.exclude_path
    plan = plan_git_exclude(location)
    assert plan.action == "already-ignored" and plan.exclude_present is True
    if replacement == "regular":
        candidate = tmp_path / "replacement-regular"
        candidate.write_bytes(b"replacement\n")
    elif replacement == "symlink":
        candidate = tmp_path / "replacement-symlink"
        candidate.symlink_to(tmp_path / "foreign")
    elif replacement == "directory":
        candidate = tmp_path / "replacement-directory"
        candidate.mkdir()
    exclude.unlink()
    if replacement != "missing":
        os.replace(candidate, exclude)
        assert (exclude.lstat().st_dev, exclude.lstat().st_ino) != (
            plan.exclude_device, plan.exclude_inode
        )

    baseline = descriptor_count()
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)

    assert failure.value.code == "stale_state_binding"
    assert descriptor_count() == baseline
    assert not exclude.with_name("exclude.localsetup.lock").exists()


@pytest.mark.parametrize("failure", ["mode", "utf8"])
def test_already_ignored_live_file_validation_fails_without_exclude_write(
    tmp_path: Path, failure: str
) -> None:
    repo = repository(tmp_path / "repo")
    (repo / ".gitignore").write_text("/.codex/state/\n", encoding="utf-8")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    assert location.git
    exclude = location.git.exclude_path
    plan = plan_git_exclude(location)
    assert plan.action == "already-ignored"
    if failure == "mode":
        exclude.chmod(0o666)
    else:
        exclude.write_bytes(exclude.read_bytes() + b"\xff")
    before = exclude.read_bytes()
    baseline = descriptor_count()

    with pytest.raises(ClientStateError) as error:
        apply_git_exclude(plan)

    assert error.value.code == "unsafe_exclude"
    assert exclude.read_bytes() == before
    assert descriptor_count() == baseline


def _bound_repo_child_location(repo: Path, home: Path):
    location = resolve_state_location(
        ROOT,
        "codex/codex-cli",
        cwd=repo,
        home=home,
        scope="repo",
        child="agent-routing",
    )
    return artifacts.bind_state_location(location)


def _replace_bound_child(location, target: str, outside: Path) -> Path:
    if target == "parent":
        prior = location.parent_root.with_name(f"{location.parent_root.name}-prior")
        location.parent_root.rename(prior)
        location.parent_root.symlink_to(outside, target_is_directory=True)
        return prior / location.child
    prior = location.root.with_name(f"{location.root.name}-prior")
    location.root.rename(prior)
    location.root.symlink_to(outside, target_is_directory=True)
    return prior


def test_child_linearized_plan_requires_a_bound_repo_child_and_preserves_strict_default(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path / "repo")
    home = tmp_path / "home"
    parent = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=home, scope="repo"
    )
    raw_child = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=home, scope="repo", child="agent-routing"
    )
    global_child = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=home, scope="global", child="agent-routing"
    )

    assert plan_git_exclude(parent).state_binding_mode == "strict"
    for location in (parent, raw_child, global_child):
        with pytest.raises(ClientStateError) as failure:
            plan_git_exclude(location, child_linearized=True)
        assert failure.value.code == "stale_state_binding"

    bound = _bound_repo_child_location(repo, home)
    special = plan_git_exclude(bound, child_linearized=True)
    assert special.state_binding_mode == "child-linearized"
    assert special.entry == "/.codex/state/agent-routing/"


@pytest.mark.parametrize("exclude_present", [False, True])
@pytest.mark.parametrize("target", ["parent", "child"])
def test_child_linearized_precommit_swap_preserves_exclude_and_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exclude_present: bool,
    target: str,
) -> None:
    repo = repository(tmp_path / "repo")
    location = _bound_repo_child_location(repo, tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not exclude_present:
        exclude.unlink()
    before = exclude.read_bytes() if exclude_present else None
    plan = plan_git_exclude(location, child_linearized=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = git_exclude._require_state_binding
    checks = 0
    final_check = 5 if exclude_present else 3

    def replace_before_final_state_check(candidate) -> None:
        nonlocal checks
        checks += 1
        if checks == final_check:
            _replace_bound_child(location, target, outside)
        original(candidate)

    monkeypatch.setattr(git_exclude, "_require_state_binding", replace_before_final_state_check)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)

    assert failure.value.code in {"stale_state_binding", "unsafe_state_path"}
    assert checks == final_check
    assert (exclude.read_bytes() if exclude.exists() else None) == before
    assert b"/.codex/state/\n" not in (exclude.read_bytes() if exclude.exists() else b"")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("exclude_present", [False, True])
@pytest.mark.parametrize("target", ["parent", "child"])
def test_child_linearized_postwrite_swap_is_committed_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exclude_present: bool,
    target: str,
) -> None:
    repo = repository(tmp_path / "repo")
    location = _bound_repo_child_location(repo, tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not exclude_present:
        exclude.unlink()
    before = exclude.read_bytes() if exclude_present else b""
    plan = plan_git_exclude(location, child_linearized=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_effective_ignore = git_exclude._effective_ignore
    injected = False

    def confirm_then_replace(git_root: Path, entry: str) -> bool:
        nonlocal injected
        ignored = original_effective_ignore(git_root, entry)
        if not injected and ignored and entry == plan.entry:
            injected = True
            _replace_bound_child(location, target, outside)
        return ignored

    monkeypatch.setattr(git_exclude, "_effective_ignore", confirm_then_replace)
    applied = apply_git_exclude(plan)

    assert injected and applied.action == "applied"
    final = exclude.read_bytes()
    assert final == before + plan.entry.encode("utf-8") + b"\n"
    assert b"/.codex/state/\n" not in final
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("exclude_present", [False, True])
@pytest.mark.parametrize("target", ["lock", "exclude"])
def test_child_linearized_postwrite_git_binding_failure_stays_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exclude_present: bool,
    target: str,
) -> None:
    repo = repository(tmp_path / "repo")
    location = _bound_repo_child_location(repo, tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not exclude_present:
        exclude.unlink()
    before = exclude.read_bytes() if exclude_present else b""
    plan = plan_git_exclude(location, child_linearized=True)
    lock = exclude.with_name("exclude.localsetup.lock")
    original_write = os.write
    injected = False

    def append_then_replace_git_binding(fd: int, data: bytes) -> int:
        nonlocal injected
        written = original_write(fd, data)
        if not injected and plan.entry.encode("utf-8") in data:
            injected = True
            replacement = tmp_path / f"replacement-{target}"
            replacement.write_bytes(b"replacement\n")
            os.replace(replacement, lock if target == "lock" else exclude)
        return written

    monkeypatch.setattr(git_exclude.os, "write", append_then_replace_git_binding)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)

    assert injected and failure.value.code == "exclude_commit_ambiguous"
    if target == "lock":
        assert exclude.read_bytes() == before + plan.entry.encode("utf-8") + b"\n"
    else:
        assert exclude.read_bytes() == b"replacement\n"


@pytest.mark.parametrize("exclude_present", [False, True])
@pytest.mark.parametrize("target", ["parent", "child"])
def test_child_linearized_already_ignored_final_check_is_strict_and_no_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exclude_present: bool,
    target: str,
) -> None:
    repo = repository(tmp_path / "repo")
    location = _bound_repo_child_location(repo, tmp_path / "home")
    assert location.git
    exclude = location.git.exclude_path
    if not exclude_present:
        exclude.unlink()
    before = exclude.read_bytes() if exclude_present else None
    (repo / ".gitignore").write_text("/.codex/state/agent-routing/\n", encoding="utf-8")
    plan = plan_git_exclude(location, child_linearized=True)
    assert plan.action == "already-ignored"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = git_exclude._require_state_binding
    checks = 0
    final_check = 5 if exclude_present else 3

    def replace_after_ignore_probe(candidate) -> None:
        nonlocal checks
        checks += 1
        if checks == final_check:
            _replace_bound_child(location, target, outside)
        original(candidate)

    monkeypatch.setattr(git_exclude, "_require_state_binding", replace_after_ignore_probe)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)

    assert failure.value.code in {"stale_state_binding", "unsafe_state_path"}
    assert checks == final_check
    assert (exclude.read_bytes() if exclude.exists() else None) == before
    assert list(outside.iterdir()) == []


def test_artifact_allocation_is_exclusive_deterministic_and_private(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    now = datetime(2026, 7, 15, 16, 2, 3, 456789, tzinfo=timezone.utc)
    first = allocate_artifact(
        location,
        content=b"checkpoint\n",
        agent="controller",
        purpose="release-checkpoint",
        extension="md",
        kind="restart-artifact",
        schema="restart-v1",
        producer="codex-controller",
        consumers=["codex", "codex"],
        now=now,
    )
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    second = allocate_artifact(
        location,
        content=b"next\n",
        agent="controller",
        purpose="release-checkpoint",
        extension="md",
        kind="restart-artifact",
        schema="restart-v1",
        producer="codex-controller",
        now=now,
    )
    assert first["artifact"] == "controller-20260715T160203456Z-release-checkpoint.md"
    assert second["artifact"] == "controller-20260715T160203456Z-release-checkpoint-01.md"
    encoded = json.dumps(first)
    assert str(repo) not in encoded and str(tmp_path) not in encoded
    assert first["record"]["repository"]["root"] == "."
    assert first["record"]["content"]["sha256"]
    assert first["record"]["consumers"] == ["codex"]


@pytest.mark.parametrize(
    ("agent_mode", "expected_agent"),
    [("omitted", "codex-cli"), ("none", "codex-cli"), ("explicit", "worker")],
)
def test_artifact_agent_defaults_only_when_omitted_or_none(
    tmp_path: Path, agent_mode: str, expected_agent: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    options = artifact_options()
    options["now"] = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    if agent_mode == "none":
        options["agent"] = None
    elif agent_mode == "explicit":
        options["agent"] = expected_agent
    allocated = allocate_artifact(location, **options)
    assert parse_artifact_name(allocated["artifact"]).agent == expected_agent
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)["ok"]


def test_explicit_empty_agent_fails_before_exclude_or_state_mutation(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    before = exclude_bytes(repo)
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(location, agent="", **artifact_options())
    assert failure.value.code == "invalid_agent"
    assert exclude_bytes(repo) == before
    assert not (repo / ".codex" / "state").exists()


@pytest.mark.parametrize("scope", ["repo", "global"])
def test_two_process_fresh_root_allocation_waits_and_uses_deterministic_suffix(
    tmp_path: Path, scope: str
) -> None:
    if scope == "repo":
        cwd = repository(tmp_path / "repo")
        home = tmp_path / "home"
        codex_home = None
        state = cwd / ".codex" / "state"
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = tmp_path / "home"
        codex_owner = tmp_path / "codex-home"
        codex_owner.mkdir()
        codex_home = str(codex_owner)
        state = codex_owner / "state"
    ready, exitcodes, outcomes = concurrent_allocation_outcomes(
        cwd, home, scope, (codex_home, codex_home)
    )
    assert ready and exitcodes == [0, 0], (ready, exitcodes, outcomes)
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    assert len(outcomes) == 2, outcomes
    assert sorted(outcome[1] for outcome in outcomes) == [
        "codex-cli-20260715T120000000Z-same-time-01.md",
        "codex-cli-20260715T120000000Z-same-time.md",
    ]
    assert len(list(state.glob("*.meta.json"))) == 2
    assert not list(state.glob(".localsetup-pending-*"))


def test_two_process_fresh_root_prebarrier_failure_releases_peer(tmp_path: Path) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    codex_owner = tmp_path / "codex-home"
    codex_owner.mkdir()
    ready, exitcodes, outcomes = concurrent_allocation_outcomes(
        cwd, tmp_path / "home", "global", ("relative-codex-home", str(codex_owner))
    )
    assert not ready and exitcodes == [0, 0], (ready, exitcodes, outcomes)
    assert sorted(outcomes) == [
        ("error", "BrokenBarrierError", None),
        ("error", "ClientStateError", "invalid_environment"),
    ]
    assert not (codex_owner / "state").exists()


@pytest.mark.parametrize("scope", ["repo", "global"])
def test_allocation_lock_default_waits_for_real_holder_release(
    tmp_path: Path, scope: str
) -> None:
    context = multiprocessing.get_context("spawn")
    if scope == "repo":
        cwd = repository(tmp_path / "repo")
        home = tmp_path / "home"
        codex_home = None
        state = cwd / ".codex" / "state"
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = tmp_path / "home"
        codex_owner = tmp_path / "codex-home"
        codex_owner.mkdir()
        codex_home = str(codex_owner)
        state = codex_owner / "state"
    lock_acquired = context.Event()
    contender_attempted = context.Event()
    holder_results = context.Queue()
    contender_results = context.Queue()
    holder = context.Process(
        target=held_allocation_lock_worker,
        args=(
            str(cwd),
            str(home),
            scope,
            codex_home,
            lock_acquired,
            contender_attempted,
            holder_results,
        ),
    )
    contender = context.Process(
        target=allocation_contender_worker,
        args=(str(cwd), str(home), scope, codex_home, contender_attempted, contender_results),
    )
    try:
        holder.start()
        holder_ready = lock_acquired.wait(timeout=PROCESS_STARTUP_SECONDS)
        if holder_ready:
            contender.start()
        contender_ready = contender_attempted.wait(timeout=PROCESS_STARTUP_SECONDS) if holder_ready else False
        deadline = time.monotonic() + PROCESS_ALLOCATION_SECONDS if contender_ready else time.monotonic()
        for process in (holder, contender):
            if process.pid is not None:
                process.join(timeout=max(0, deadline - time.monotonic()))
        exitcodes = (holder.exitcode, contender.exitcode)
        outcomes = []
        for results in (holder_results, contender_results):
            try:
                outcomes.append(results.get(timeout=0.25))
            except Empty:
                outcomes.append(None)
    finally:
        reap_allocation_processes((holder, contender), holder_results, contender_results)
    assert holder_ready and contender_ready and exitcodes == (0, 0), (
        holder_ready, contender_ready, exitcodes, outcomes
    )
    assert outcomes[0] == ("ok",), outcomes
    assert outcomes[1] == (
        "ok",
        "codex-cli-20260715T120000000Z-same-time.md",
    ), outcomes
    assert len(list(state.glob("*.meta.json"))) == 1
    assert not list(state.glob(".localsetup-pending-*"))


def test_allocation_lock_default_uses_blocking_flock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def record(lock_fd: int, operation: int) -> None:
        calls.append((lock_fd, operation))

    monkeypatch.setattr(artifacts.fcntl, "flock", record)
    artifacts._acquire_allocation_lock(1)
    assert calls == [(1, fcntl.LOCK_EX)]


def test_allocation_lock_explicit_timeout_is_bounded_and_typed(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    sleeps: list[float] = []
    clock = iter([0.0, 0.01, 0.02, 0.03])

    def busy(*_args) -> None:
        nonlocal attempts
        attempts += 1
        raise BlockingIOError

    monkeypatch.setattr(artifacts.fcntl, "flock", busy)
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(artifacts.time, "sleep", sleeps.append)
    with pytest.raises(ClientStateError) as failure:
        artifacts._acquire_allocation_lock(1, timeout=0.02, poll=0.01)
    assert failure.value.code == "artifact_locked"
    assert attempts == 2 and sleeps == [0.01]


@pytest.mark.parametrize("field", ["predecessor", "checkpoint"])
def test_relative_metadata_length_boundary_is_prevalidated(
    tmp_path: Path, field: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    accepted = prepare_artifact_request(
        location, content=b"boundary\n", purpose="boundary", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
        **{field: "a" * 512},
    )
    allocated = allocate_artifact(location, prepared=accepted)
    assert verify_artifact(location, allocated["artifact"], schema_path=SCHEMA)["ok"]

    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError) as failure:
        prepare_artifact_request(
            current, content=b"", purpose="boundary", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
            **{field: "a" * 513},
        )
    assert failure.value.code == f"invalid_{field}"


def test_relative_metadata_schema_matches_runtime_normalization(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"schema\n", purpose="schema-path", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    metadata_schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    for field in ("predecessor", "checkpoint"):
        for accepted in ACCEPTED_RELATIVE_PATHS:
            assert not artifacts._schema_issues(
                {**allocated["record"], field: accepted}, metadata_schema
            ), (field, accepted)
        for rejected in REJECTED_RELATIVE_PATHS:
            assert artifacts._schema_issues(
                {**allocated["record"], field: rejected}, metadata_schema
            ), (field, rejected)


def test_artifact_filename_schema_matches_runtime_collision_suffixes(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    allocated = allocate_artifact(location, **artifact_options())
    metadata_schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    bare = allocated["record"]
    zero = {**bare, "artifact": bare["artifact"].replace(".md", "-00.md")}
    first = {**bare, "artifact": bare["artifact"].replace(".md", "-01.md")}
    last = {**bare, "artifact": bare["artifact"].replace(".md", "-99.md")}

    assert not artifacts._schema_issues(bare, metadata_schema)
    assert artifacts._schema_issues(zero, metadata_schema)
    assert not artifacts._schema_issues(first, metadata_schema)
    assert not artifacts._schema_issues(last, metadata_schema)


def test_preflight_opens_existing_state_root_read_only_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(
        ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home"
    )
    request = prepare_artifact_request(location, **artifact_options())
    original = artifacts._open_location_directory
    calls: list[bool] = []

    def record_open(bound, *, create: bool, **kwargs):
        calls.append(create)
        return original(bound, create=create, **kwargs)

    monkeypatch.setattr(artifacts, "_open_location_directory", record_open)
    before_fds = descriptor_count()
    before_mode = stat.S_IMODE(state.stat().st_mode)
    artifacts.preflight_artifact_request(location, request)

    assert calls == [False]
    assert descriptor_count() == before_fds
    assert stat.S_IMODE(state.stat().st_mode) == before_mode


@pytest.mark.parametrize("field", ["predecessor", "checkpoint"])
@pytest.mark.parametrize("value", ["foo/", "."])
def test_verify_rejects_non_normalized_path_in_tampered_metadata(
    tmp_path: Path, field: str, value: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"tamper\n", purpose="tampered-path", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    metadata_path = location.root / allocated["metadata"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[field] = value
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ClientStateError) as failure:
        verify_artifact(location, allocated["artifact"], schema_path=SCHEMA)
    assert failure.value.code == "invalid_metadata"


def test_oversized_metadata_is_rejected_before_direct_api_mutation(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    consumers = [f"c{index:05d}-" + ("a" * 55) for index in range(18000)]
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(
            location, content=b"x\n", purpose="consumers", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
            consumers=reversed(consumers),
        )
    assert failure.value.code == "invalid_metadata"
    assert not (repo / ".codex" / "state").exists()


def test_explicit_global_scope_path_allocate_verify_from_deleted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = tmp_path / "deleted-cwd"
    dead.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    prior_fd = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.chdir(dead)
        os.rmdir(dead)
        location = resolve_state_location(
            ROOT, "codex/codex-cli", cwd=Path("."), home=home, scope="global"
        )
        assert location.scope == "global" and location.cwd == ROOT
        allocated = allocate_artifact(
            location, content=b"global\n", purpose="deleted-cwd", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
        assert verify_artifact(location, allocated["artifact"], schema_path=SCHEMA)["ok"]
    finally:
        os.fchdir(prior_fd)
        os.close(prior_fd)


def test_artifact_validation_and_stale_bindings(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location,
        content=b"accepted\n",
        purpose="accepted-checkpoint",
        extension="json",
        kind="controller-ledger",
        schema="ledger-v1",
        producer="controller",
    )
    verified = verify_artifact(location, allocated["artifact"], schema_path=SCHEMA)
    assert verified["ok"] and verified["failures"] == []

    (location.root / allocated["artifact"]).write_bytes(b"tampered\n")
    verified = verify_artifact(location, allocated["artifact"], schema_path=SCHEMA)
    assert not verified["ok"] and "content hash mismatch" in verified["failures"]

    (repo / "tracked").write_text("two\n", encoding="utf-8")
    git(repo, "commit", "-qam", "advance")
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    stale = verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)
    assert "repository/ref snapshot is stale" in stale["failures"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("agent", "Bad Agent", "agent"),
        ("purpose", "../escape", "purpose"),
        ("extension", "md.exe", "extension"),
        ("predecessor", "../secret", "predecessor"),
    ],
)
def test_artifact_identity_and_paths_are_validated(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    kwargs = dict(
        content=b"",
        agent="worker",
        purpose="handoff",
        extension="md",
        kind="restart-artifact",
        schema="restart-v1",
        producer="worker",
    )
    kwargs[field] = value
    with pytest.raises(ClientStateError, match=message):
        allocate_artifact(location, **kwargs)


def test_state_root_inode_swap_is_rejected_before_write(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    state.rename(repo / ".codex" / "prior-state")
    state.mkdir()
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert list(state.iterdir()) == []


def test_ineffective_whitespace_exclude_is_not_treated_as_covered(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    location.git.exclude_path.write_text("  /.codex/state/\n", encoding="utf-8")
    plan = plan_git_exclude(location)
    assert plan.action == "append"
    apply_git_exclude(plan)
    assert plan_git_exclude(location).action == "already-ignored"


def test_stale_location_rejects_allocate_after_ref_advance(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    (repo / "tracked").write_text("advanced\n", encoding="utf-8")
    git(repo, "commit", "-qam", "advance")
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert not (repo / ".codex" / "state").exists()


@pytest.mark.parametrize(
    "value",
    [
        "agent-20260230T120000000Z-purpose.md",
        "agent-20260715T250000000Z-purpose.md",
        "agent-20260715T120000000Z-purpose-00.md",
        "agent-20260715T12000000Z-purpose.md",
        "Agent-20260715T120000000Z-purpose.md",
    ],
)
def test_filename_parser_rejects_malformed_and_impossible_values(value: str) -> None:
    with pytest.raises(ClientStateError):
        parse_artifact_name(value)


@pytest.mark.parametrize("field", ["predecessor", "checkpoint"])
@pytest.mark.parametrize("value", ACCEPTED_RELATIVE_PATHS)
def test_metadata_paths_accept_normalized_posix_grammar(
    tmp_path: Path, field: str, value: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    prepared = prepare_artifact_request(
        location, content=b"", purpose="handoff", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
        **{field: value},
    )
    assert getattr(prepared, field) == value


@pytest.mark.parametrize("field", ["predecessor", "checkpoint"])
@pytest.mark.parametrize("value", REJECTED_RELATIVE_PATHS)
def test_metadata_paths_reject_non_normalized_posix_grammar(
    tmp_path: Path, field: str, value: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError, match="POSIX-relative") as failure:
        prepare_artifact_request(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
            **{field: value},
        )
    assert failure.value.code == f"invalid_{field}"


@pytest.mark.parametrize(
    "schema_kind", ["missing", "directory", "symlink", "unreadable", "fifo", "socket", "device"]
)
def test_metadata_schema_fails_closed(tmp_path: Path, schema_kind: str) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    schema = tmp_path / "schema.json"
    if schema_kind == "directory":
        schema.mkdir()
    elif schema_kind == "symlink":
        schema.symlink_to(SCHEMA)
    elif schema_kind == "unreadable":
        schema.write_text("{}\n", encoding="utf-8")
        schema.chmod(0)
    elif schema_kind == "fifo":
        os.mkfifo(schema)
    elif schema_kind == "socket":
        handle = socket.socket(socket.AF_UNIX)
        handle.bind(str(schema))
        handle.close()
    elif schema_kind == "device":
        schema = Path("/dev/null")
    started = time.monotonic()
    with pytest.raises(ClientStateError, match="schema"):
        prepare_artifact_request(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
            metadata_schema=schema,
        )
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("schema_kind", ["directory", "symlink", "fifo", "socket", "device"])
def test_verify_schema_type_fails_promptly_without_state_residue(
    tmp_path: Path, schema_kind: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(location, **artifact_options())
    schema = tmp_path / "verify-schema"
    if schema_kind == "directory":
        schema.mkdir()
    elif schema_kind == "symlink":
        schema.symlink_to(SCHEMA)
    elif schema_kind == "fifo":
        os.mkfifo(schema)
    elif schema_kind == "socket":
        handle = socket.socket(socket.AF_UNIX)
        handle.bind(str(schema))
        handle.close()
    else:
        schema = Path("/dev/null")
    before = sorted(path.name for path in location.root.iterdir())
    started = time.monotonic()
    with pytest.raises(ClientStateError) as failure:
        verify_artifact(location, allocated["artifact"], schema_path=schema)
    assert failure.value.code == "invalid_metadata_schema"
    assert time.monotonic() - started < 1
    assert sorted(path.name for path in location.root.iterdir()) == before


def test_same_key_registry_drift_invalidates_location(tmp_path: Path) -> None:
    source = registry_root(tmp_path / "source")
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(source, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    clients = source / "ls" / "config" / "clients.yaml"
    text = clients.read_text(encoding="utf-8")
    clients.write_text(text.replace("display_name: OpenAI Codex CLI", "display_name: OpenAI Codex Command Line", 1), encoding="utf-8")
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )


def test_state_root_swap_between_refresh_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original = artifacts._open_absolute_directory

    def swap_then_open(path: Path, *, create: bool, **kwargs) -> int:
        state.rename(repo / ".codex" / "prior-state")
        state.mkdir()
        return original(path, create=create, **kwargs)

    monkeypatch.setattr(artifacts, "_open_absolute_directory", swap_then_open)
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert list(state.iterdir()) == []


def test_metadata_timestamp_and_registry_digest_are_bound(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"bound\n", purpose="handoff", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    client = allocated["record"]["client"]
    assert client["registry_schema_version"] == 1
    assert client["variant_digest"] == location.variant_digest
    sidecar = location.root / allocated["metadata"]
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    metadata["created_at"] = "20260715T120001000Z"
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    result = verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)
    assert result["failures"] == ["artifact timestamp mismatch"]


def test_state_root_swap_after_bound_write_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original = artifacts._exclusive_write
    calls = 0

    def swap_during_artifact(directory_fd: int, name: str, data: bytes):
        nonlocal calls
        calls += 1
        if calls == 2:
            state.rename(repo / ".codex" / "prior-state")
            state.mkdir()
        return original(directory_fd, name, data)

    monkeypatch.setattr(artifacts, "_exclusive_write", swap_during_artifact)
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"bound\n", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    assert list(state.iterdir()) == []
    assert [path.name for path in (repo / ".codex" / "prior-state").iterdir()] == [
        ".localsetup-artifacts.lock"
    ]


def test_ref_advance_during_write_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original = artifacts._exclusive_write
    calls = 0

    def advance_during_artifact(directory_fd: int, name: str, data: bytes):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "tracked").write_text("advanced during allocation\n", encoding="utf-8")
            git(repo, "commit", "-qam", "advance during allocation")
        return original(directory_fd, name, data)

    monkeypatch.setattr(artifacts, "_exclusive_write", advance_during_artifact)
    with pytest.raises(ClientStateError, match="stale"):
        allocate_artifact(
            location, content=b"bound\n", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert [path.name for path in (repo / ".codex" / "state").iterdir()] == [
        ".localsetup-artifacts.lock"
    ]


def test_git_exclude_parent_swap_is_descriptor_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    info = location.git.exclude_path.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    original = git_exclude._open_directory

    def swap_after_open(path: Path, *, create: bool) -> int:
        directory_fd = original(path, create=create)
        info.rename(info.with_name("info-prior"))
        info.symlink_to(outside, target_is_directory=True)
        return directory_fd

    monkeypatch.setattr(git_exclude, "_open_directory", swap_after_open)
    with pytest.raises(ClientStateError, match="unsafe|binding"):
        apply_git_exclude(plan)
    assert not (outside / "exclude").exists()


def test_git_exclude_failed_postcheck_preserves_owned_tail_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    before = location.git.exclude_path.read_bytes()
    monkeypatch.setattr(git_exclude, "_effective_ignore", lambda *_args: False)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert location.git.exclude_path.read_bytes() == before + b"/.codex/state/\n"


def test_git_exclude_stale_lock_file_is_reusable(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    lock = location.git.exclude_path.with_name(f"{location.git.exclude_path.name}.localsetup.lock")
    lock.write_bytes(b"stale process residue\n")
    assert apply_git_exclude(plan).action == "applied"


def test_git_exclude_short_write_preserves_partial_bytes_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    before = location.git.exclude_path.read_bytes()
    original = os.write
    injected = False

    def short_write(fd: int, data: bytes) -> int:
        nonlocal injected
        if not injected and b"/.codex/state/" in data:
            injected = True
            return original(fd, data[:-1])
        return original(fd, data)

    monkeypatch.setattr(git_exclude.os, "write", short_write)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    assert location.git.exclude_path.read_bytes() == before + b"/.codex/state/"


def test_git_exclude_foreign_append_is_preserved_and_digest_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    exclude = location.git.exclude_path
    original = os.write
    injected = False

    def append_foreign_first(fd: int, data: bytes) -> int:
        nonlocal injected
        if not injected and b"/.codex/state/" in data:
            injected = True
            foreign_fd = os.open(exclude, os.O_WRONLY | os.O_APPEND)
            try:
                original(foreign_fd, b"/foreign/\n")
            finally:
                os.close(foreign_fd)
        return original(fd, data)

    monkeypatch.setattr(git_exclude.os, "write", append_foreign_first)
    applied = apply_git_exclude(plan)
    final = exclude.read_bytes()
    assert b"/foreign/\n" in final and b"/.codex/state/\n" in final
    import hashlib
    assert applied.expected_digest == hashlib.sha256(final).hexdigest()


def test_artifact_and_state_modes_are_exact_under_restrictive_umask(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    previous = os.umask(0o777)
    try:
        allocated = allocate_artifact(
            location, content=b"private\n", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    finally:
        os.umask(previous)
    state = repo / ".codex" / "state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / allocated["artifact"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((state / allocated["metadata"]).stat().st_mode) == 0o600
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)["ok"]


def test_interrupted_sidecar_write_cleans_pending_and_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original = artifacts._exclusive_write
    calls = 0

    def interrupt_sidecar(directory_fd: int, name: str, data: bytes):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return original(directory_fd, name, data)

    monkeypatch.setattr(artifacts, "_exclusive_write", interrupt_sidecar)
    with pytest.raises(KeyboardInterrupt):
        allocate_artifact(
            location, content=b"private\n", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert [path.name for path in (repo / ".codex" / "state").iterdir()] == [
        ".localsetup-artifacts.lock"
    ]


def test_pending_receipt_recovers_owned_orphan_and_preserves_foreign_collision(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    name = "controller-20260715T120000000Z-recovery.md"
    metadata_name = f"{name}.meta.json"
    content = b"owned orphan\n"
    metadata = b"{}\n"
    pending = artifacts._pending_payload(name, metadata_name, content, metadata)
    receipt = state / artifacts._pending_name(name)
    receipt.write_bytes(pending)
    receipt.chmod(0o600)
    artifact = state / name
    artifact.write_bytes(content)
    artifact.chmod(0o600)
    allocate_artifact(
        location, content=b"new\n", purpose="new-handoff", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    assert not receipt.exists() and not artifact.exists()

    receipt.write_bytes(pending)
    receipt.chmod(0o600)
    artifact.write_bytes(b"foreign collision\n")
    artifact.chmod(0o600)
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError, match="foreign"):
        allocate_artifact(
            current, content=b"next\n", purpose="next-handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert receipt.exists() and artifact.read_bytes() == b"foreign collision\n"


def test_directory_fsync_failure_is_ambiguous_and_next_run_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    (repo / ".codex" / "state").mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original = os.fsync
    directory_calls = 0

    def fail_second_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 2:
                raise OSError("injected directory fsync failure")
        original(fd)

    monkeypatch.setattr(artifacts.os, "fsync", fail_second_directory_fsync)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(
            location, content=b"committed pair\n", purpose="recovery", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller", now=now,
        )
    assert failure.value.code == "artifact_commit_ambiguous"
    state = repo / ".codex" / "state"
    assert len(list(state.glob(".localsetup-pending-*"))) == 1

    monkeypatch.setattr(artifacts.os, "fsync", original)
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    recovered = allocate_artifact(
        current, content=b"committed pair\n", purpose="recovery", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller", now=now,
    )
    assert recovered["artifact"].endswith("-01.md")
    assert list(state.glob(".localsetup-pending-*")) == []
    assert (state / "codex-cli-20260715T120000000Z-recovery.md").exists()


def test_created_directory_fsyncs_child_then_open_parent_at_each_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    target = base / "one" / "two"
    real_fsync = os.fsync
    events: list[tuple[int, int]] = []

    def record(fd: int) -> None:
        details = os.fstat(fd)
        events.append((details.st_dev, details.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", record)
    fd = artifacts._open_absolute_directory(target, create=True)
    os.close(fd)
    assert events == [
        (target.parent.stat().st_dev, target.parent.stat().st_ino),
        (base.stat().st_dev, base.stat().st_ino),
        (target.stat().st_dev, target.stat().st_ino),
        (target.parent.stat().st_dev, target.parent.stat().st_ino),
    ]


@pytest.mark.parametrize("failure_call", [1, 2, 3, 4])
def test_created_directory_fsync_failure_is_ambiguous_and_next_run_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    target = base / "one" / "two"
    real_fsync = os.fsync
    calls = 0

    def fail_selected(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("injected directory durability failure")
        real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", fail_selected)
    with pytest.raises(ClientStateError) as failure:
        artifacts._open_absolute_directory(target, create=True)
    assert failure.value.code == "artifact_commit_ambiguous"
    monkeypatch.setattr(artifacts.os, "fsync", real_fsync)
    fd = artifacts._open_absolute_directory(target, create=True)
    try:
        assert os.fstat(fd).st_uid == os.geteuid()
    finally:
        os.close(fd)


def test_verify_rejects_root_swap_after_descriptor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"verify me\n", purpose="verify", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    state = repo / ".codex" / "state"
    original = artifacts._read_regular_with_identity
    reads = 0

    def swap_after_reads(directory_fd: int, name: str, *, maximum: int):
        nonlocal reads
        data = original(directory_fd, name, maximum=maximum)
        reads += 1
        if reads == 2:
            state.rename(repo / ".codex" / "prior-state")
            state.mkdir(mode=0o700)
        return data

    monkeypatch.setattr(artifacts, "_read_regular_with_identity", swap_after_reads)
    with pytest.raises(ClientStateError, match="stale"):
        verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)


def test_git_exclude_foreign_tail_on_failed_verification_is_preserved_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    exclude = location.git.exclude_path
    checks = 0

    def fail_after_foreign_append(_root: Path, _entry: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            with exclude.open("ab") as handle:
                handle.write(b"/foreign-after/\n")
            return False
        return False

    monkeypatch.setattr(git_exclude, "_effective_ignore", fail_after_foreign_append)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    assert failure.value.code == "exclude_commit_ambiguous"
    final = exclude.read_bytes()
    assert b"/.codex/state/\n" in final and b"/foreign-after/\n" in final


def test_git_environment_overrides_cannot_redirect_f02(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repository(tmp_path / "repo")
    foreign = repository(tmp_path / "foreign")
    sentinel = foreign / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    before = sentinel.read_bytes()
    overrides = {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_COMMON_DIR": str(foreign / ".git"),
        "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
        "GIT_NAMESPACE": "hostile",
        "GIT_OBJECT_DIRECTORY": str(foreign / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / ".git" / "objects"),
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git and location.git.root == repo
    apply_git_exclude(plan_git_exclude(location))
    assert sentinel.read_bytes() == before
    assert b"/.codex/state/" not in (foreign / ".git" / "info" / "exclude").read_bytes()


@pytest.mark.parametrize("target", ["artifact-lock", "exclude-lock", "exclude"])
def test_mutable_hardlinks_are_rejected_without_foreign_mutation(tmp_path: Path, target: str) -> None:
    repo = repository(tmp_path / "repo")
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign bytes\n")
    if target == "artifact-lock":
        (repo / ".codex" / "state").mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    if target == "artifact-lock":
        state = repo / ".codex" / "state"
        os.link(foreign, state / ".localsetup-artifacts.lock")
        with pytest.raises(ClientStateError, match="lock"):
            allocate_artifact(
                location, content=b"x", purpose="handoff", extension="md",
                kind="restart-artifact", schema="restart-v1", producer="controller",
            )
    elif target == "exclude-lock":
        plan = plan_git_exclude(location)
        os.link(foreign, location.git.exclude_path.with_name("exclude.localsetup.lock"))
        with pytest.raises(ClientStateError, match="single-link"):
            apply_git_exclude(plan)
    else:
        location.git.exclude_path.unlink()
        os.link(foreign, location.git.exclude_path)
        with pytest.raises(ClientStateError, match="single-link"):
            plan_git_exclude(location)
    assert foreign.read_bytes() == b"foreign bytes\n"


@pytest.mark.parametrize("target", ["artifact-lock", "exclude-lock", "exclude"])
def test_mutable_fifos_are_rejected_before_open(tmp_path: Path, target: str) -> None:
    repo = repository(tmp_path / "repo")
    if target == "artifact-lock":
        (repo / ".codex" / "state").mkdir(parents=True)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    assert location.git
    if target == "artifact-lock":
        fifo = repo / ".codex" / "state" / ".localsetup-artifacts.lock"
        os.mkfifo(fifo)
        operation = lambda: allocate_artifact(
            location, content=b"x", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    elif target == "exclude-lock":
        plan = plan_git_exclude(location)
        fifo = location.git.exclude_path.with_name("exclude.localsetup.lock")
        os.mkfifo(fifo)
        operation = lambda: apply_git_exclude(plan)
    else:
        location.git.exclude_path.unlink()
        os.mkfifo(location.git.exclude_path)
        operation = lambda: plan_git_exclude(location)
    with pytest.raises(ClientStateError, match="regular|lock"):
        operation()


@pytest.mark.parametrize("probe_number", [1, 2])
def test_late_git_info_redirect_is_rejected_after_ignore_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_number: int
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    plan = plan_git_exclude(location)
    assert location.git
    info = location.git.exclude_path.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = 0

    def redirect(_root: Path, _entry: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == probe_number:
            info.rename(info.with_name("info-prior"))
            info.symlink_to(outside, target_is_directory=True)
            (outside / "exclude").write_text("/.codex/state/\n", encoding="utf-8")
        return calls == probe_number

    monkeypatch.setattr(git_exclude, "_effective_ignore", redirect)
    with pytest.raises(ClientStateError) as failure:
        apply_git_exclude(plan)
    expected = "stale_state_binding" if probe_number == 1 else "exclude_commit_ambiguous"
    assert failure.value.code == expected
    assert (outside / "exclude").read_bytes() == b"/.codex/state/\n"


@pytest.mark.parametrize("replacement", ["artifact", "metadata"])
def test_verify_rejects_same_byte_entry_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"stable\n", purpose="verify-swap", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    target_name = allocated["artifact"] if replacement == "artifact" else allocated["metadata"]
    original = artifacts._read_regular_with_identity
    swapped = False

    def replace_after_read(directory_fd: int, name: str, *, maximum: int):
        nonlocal swapped
        result = original(directory_fd, name, maximum=maximum)
        if name == target_name and not swapped:
            swapped = True
            data = result[0]
            os.unlink(name, dir_fd=directory_fd)
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory_fd)
            try:
                os.write(fd, data)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        return result

    monkeypatch.setattr(artifacts, "_read_regular_with_identity", replace_after_read)
    with pytest.raises(ClientStateError) as failure:
        verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)
    assert failure.value.code == "artifact_commit_ambiguous"


@pytest.mark.parametrize("replacement", ["artifact", "metadata"])
def test_verify_rejects_same_inode_identical_byte_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    allocated = allocate_artifact(
        location, content=b"stable\n", purpose="verify-rewrite", extension="md",
        kind="restart-artifact", schema="restart-v1", producer="controller",
    )
    current = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    target_name = allocated["artifact"] if replacement == "artifact" else allocated["metadata"]
    original = artifacts._read_regular_with_identity
    rewritten = False

    def rewrite_after_read(directory_fd: int, name: str, *, maximum: int):
        nonlocal rewritten
        result = original(directory_fd, name, maximum=maximum)
        if name == target_name and not rewritten:
            rewritten = True
            data, before = result
            after = before
            for _attempt in range(100):
                fd = os.open(
                    name, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    offset = 0
                    while offset < len(data):
                        offset += os.write(fd, data[offset:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if after.st_ctime_ns != before.st_ctime_ns:
                    break
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert after.st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(artifacts, "_read_regular_with_identity", rewrite_after_read)
    with pytest.raises(ClientStateError) as failure:
        verify_artifact(current, allocated["artifact"], schema_path=SCHEMA)
    assert failure.value.code == "artifact_commit_ambiguous"


def test_pending_receipt_limit_stops_at_101_without_reading_receipts(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    state = repo / ".codex" / "state"
    state.mkdir(parents=True)
    for index in range(101):
        receipt = state / f".localsetup-pending-agent-20260715T120000000Z-p{index:03d}.md.json"
        receipt.write_bytes(b"not parsed\n")
        receipt.chmod(0o600)
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(
            location, content=b"x", purpose="handoff", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller",
        )
    assert failure.value.code == "artifact_recovery_required"
    assert len(list(state.glob(".localsetup-pending-*"))) == 101


@pytest.mark.parametrize("branch", ["metadata-collision", "metadata-exception", "stale-postcheck"])
def test_cleanup_preserves_foreign_replacements_and_reports_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    repo = repository(tmp_path / "repo")
    location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    state = repo / ".codex" / "state"
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    artifact_name = "codex-cli-20260715T120000000Z-cleanup.md"
    metadata_name = f"{artifact_name}.meta.json"
    if branch == "metadata-collision":
        state.mkdir(parents=True)
        collision = state / metadata_name
        collision.write_bytes(b"foreign metadata collision\n")
        collision.chmod(0o600)
        location = resolve_state_location(ROOT, "codex/codex-cli", cwd=repo, home=tmp_path / "home")
    original_write = artifacts._exclusive_write

    def replace_then_fail(directory_fd: int, name: str, data: bytes):
        if name == metadata_name and branch in {"metadata-collision", "metadata-exception"}:
            victim = artifact_name if branch == "metadata-collision" else artifacts._pending_name(artifact_name)
            os.unlink(victim, dir_fd=directory_fd)
            fd = os.open(victim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory_fd)
            try:
                os.write(fd, b"foreign replacement\n")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            if branch == "metadata-exception":
                raise RuntimeError("injected metadata failure")
        return original_write(directory_fd, name, data)

    monkeypatch.setattr(artifacts, "_exclusive_write", replace_then_fail)
    if branch == "stale-postcheck":
        original_bound = artifacts._bound_location
        checks = 0

        def replace_metadata_then_stale(bound_location, directory_fd: int):
            nonlocal checks
            checks += 1
            if checks == 3:
                os.unlink(metadata_name, dir_fd=directory_fd)
                fd = os.open(metadata_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory_fd)
                try:
                    os.write(fd, b"foreign replacement\n")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
                raise ClientStateError("client state binding is stale", code="stale_state_binding")
            return original_bound(bound_location, directory_fd)

        monkeypatch.setattr(artifacts, "_bound_location", replace_metadata_then_stale)
    with pytest.raises(ClientStateError) as failure:
        allocate_artifact(
            location, content=b"owned\n", purpose="cleanup", extension="md",
            kind="restart-artifact", schema="restart-v1", producer="controller", now=now,
        )
    assert failure.value.code == "artifact_commit_ambiguous"
    assert any(path.read_bytes() == b"foreign replacement\n" for path in state.iterdir() if path.is_file())

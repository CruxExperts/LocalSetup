from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import struct
import tarfile

import pytest

from ls.core.terminal_ops import command_scheduler as scheduler


SOURCE_ROOT = Path(__file__).resolve().parents[2]


class FakeCrontab:
    def __init__(self, content: str = "", *, present: bool = False) -> None:
        self.snapshot = scheduler.CrontabSnapshot(present, content)
        self.write_count = 0
        self.write_mode = "apply"

    def read_crontab(self) -> scheduler.CrontabSnapshot:
        return self.snapshot

    def write_crontab(self, content: str) -> scheduler.CronWriteResult:
        self.write_count += 1
        if self.write_mode in {"apply", "timeout_after_apply"}:
            self.snapshot = scheduler.CrontabSnapshot(True, content)
            return scheduler.CronWriteResult(0 if self.write_mode == "apply" else None, "")
        if self.write_mode == "leave_previous":
            return scheduler.CronWriteResult(1, "synthetic write failure")
        if self.write_mode == "unexpected":
            self.snapshot = scheduler.CrontabSnapshot(True, "other-user-change\n")
            return scheduler.CronWriteResult(None, "synthetic uncertain write")
        raise AssertionError(f"unknown fake write mode {self.write_mode}")


def _source_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "source tree"
    for path in scheduler._source_paths(SOURCE_ROOT):
        target = root / path.relative_to(SOURCE_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return root


def _install(tmp_path: Path, backend: FakeCrontab, *, grant_id: str = "grant-first") -> tuple[Path, Path, dict]:
    source = _source_fixture(tmp_path)
    state = tmp_path / "private state"
    result = scheduler.install_schedule(
        source_root=source,
        state_root=state,
        grant_id=grant_id,
        backend=backend,
    )
    return source, state, result


def _read_backup(path: Path) -> tuple[dict, str]:
    with tarfile.open(path, "r:gz") as archive:
        assert {item.name for item in archive.getmembers()} == {"metadata.json", "crontab.txt"}
        assert all(item.mode == 0o600 for item in archive.getmembers())
        metadata = archive.extractfile("metadata.json")
        content = archive.extractfile("crontab.txt")
        assert metadata is not None and content is not None
        return json.loads(metadata.read()), content.read().decode("utf-8")


def test_install_preserves_crontab_creates_private_restorable_backup_and_is_idempotent(tmp_path: Path) -> None:
    prior = "17 * * * * printf unrelated\n# operator note\n"
    backend = FakeCrontab(prior, present=True)
    source, state, installed = _install(tmp_path, backend)

    assert installed["ok"] is True
    assert installed["state"] == "enabled"
    assert installed["backup"] is not None
    backup = Path(installed["backup"])
    metadata, restored = _read_backup(backup)
    assert metadata == {"present": True, "schema_version": 1}
    assert restored == prior
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "17 * * * * printf unrelated\n# operator note\n" in backend.snapshot.content
    assert backend.snapshot.content.count("* * * * *") == 1
    assert f"binding={installed['binding_sha256']}" in backend.snapshot.content
    command = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    argv = shlex.split(command.split(" ", 5)[5])
    assert "-I" in argv and "-B" in argv
    assert "\n" not in argv[argv.index("-c") + 1]
    assert argv[0] == sys.executable
    bootstrap = Path(argv[argv.index("-c") + 2])
    assert bootstrap.parent == state / "schedules"
    assert bootstrap.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(bootstrap.read_bytes()).hexdigest() == argv[argv.index("-c") + 3]
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    record = scheduler._read_record(directory, scheduler.SCHEDULE_ID)
    assert record is not None
    assert record["binding"]["runtime"]["executable"] == sys.executable
    assert record["binding"]["runtime"]["prefix"] == sys.prefix
    assert str(source) in argv
    assert bootstrap.parent.parent == state
    assert bootstrap.name.startswith(scheduler.SCHEDULE_ID + "-")
    assert installed["binding_sha256"] in argv

    status = scheduler.status_schedule(state_root=state, source_root=source, backend=backend)
    assert status["state"] == "enabled"
    assert status["enabled"] is True
    assert status["scope"] == scheduler.RECIPE_ID
    unchanged = scheduler.install_schedule(
        source_root=source, state_root=state, grant_id="grant-first", backend=backend,
    )
    assert unchanged["changed"] is False
    assert unchanged["backup"] is None
    assert backend.write_count == 1


def test_cron_wrapper_loads_only_verified_source_from_arbitrary_working_directory(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, state, _installed = _install(tmp_path, backend)
    shadow_package = tmp_path / "ls"
    shadow_package.mkdir()
    (shadow_package / "__init__.py").write_text("raise RuntimeError('cwd package imported')\n", encoding="utf-8")

    cron_line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    wrapper = subprocess.run(
        shlex.split(cron_line.split(" ", 5)[5]),
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False,
    )
    assert wrapper.returncode == 0, wrapper.stderr
    result = json.loads(wrapper.stdout)
    assert result["state"] == "completed"
    assert result["dispatched"] is True
    assert (state / "agent-shells").exists() is False


def test_installed_cron_accepts_short_line_and_rejects_long_line_without_installing(tmp_path: Path) -> None:
    if shutil.which("crontab") is None:
        pytest.skip("native crontab is unavailable")
    backend = FakeCrontab()
    _source, state, _installed = _install(tmp_path, backend)
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    cron = scheduler.SubprocessCrontabBackend()
    line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    assert len(line.encode("utf-8")) < 1000
    cron.validate_crontab(backend.snapshot.content, directory)
    with pytest.raises(scheduler.SchedulerError, match="rejected"):
        cron.validate_crontab("* * * * * " + "x" * 4352 + "\n", directory)


def test_cron_validation_failure_precedes_binding_and_write(tmp_path: Path) -> None:
    class RejectingCrontab(FakeCrontab):
        def validate_crontab(self, content: str, directory: Path) -> None:
            raise scheduler.SchedulerError("crontab_validation_failed", "Cron rejected the entry.")

    backend = RejectingCrontab("# keep\n", present=True)
    source = _source_fixture(tmp_path)
    state = tmp_path / "private state"
    with pytest.raises(scheduler.SchedulerError, match="Cron rejected"):
        scheduler.install_schedule(
            source_root=source, state_root=state, grant_id="grant-first", backend=backend,
        )
    assert backend.write_count == 0
    assert backend.snapshot.content == "# keep\n"
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    assert scheduler._read_record(directory, scheduler.SCHEDULE_ID) is None
    assert list(directory.glob("crontab-*.tar.gz")) == []


def test_private_bootstrap_change_refuses_before_project_import(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, state, _installed = _install(tmp_path, backend)
    cron_line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    argv = shlex.split(cron_line.split(" ", 5)[5])
    bootstrap = Path(argv[argv.index("-c") + 2])
    bootstrap.write_text("raise RuntimeError('unexpected bootstrap')\n", encoding="utf-8")
    wrapper = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert wrapper.returncode != 0
    assert "unexpected bootstrap" not in wrapper.stderr
    assert scheduler.status_schedule(state_root=state, source_root=source, backend=backend)["state"] == "bootstrap_drift"


def test_source_drift_blocks_dispatch_and_cannot_be_rebound_to_the_same_grant(tmp_path: Path) -> None:
    backend = FakeCrontab("# keep\n", present=True)
    source, state, installed = _install(tmp_path, backend)
    calls: list[str] = []
    service_file = source / "ls/core/terminal_ops/command_service.py"
    service_file.write_bytes(service_file.read_bytes() + b"\n# post-install edit\n")

    cron_line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    cron_argv = shlex.split(cron_line.split(" ", 5)[5])
    wrapper = subprocess.run(
        cron_argv, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False,
    )
    assert wrapper.returncode == 78
    assert wrapper.stderr.strip() == "LocalSetup scheduled GC refused: source binding mismatch or unsafe source."

    status = scheduler.status_schedule(state_root=state, source_root=source, backend=backend)
    assert status["state"] == "source_drift"
    run = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID,
        source_root=source,
        state_root=state,
        binding_sha256=installed["binding_sha256"],
        gc_callback=lambda: calls.append("gc") or {"ok": True},
    )
    assert run["state"] == "binding_refused"
    assert calls == []
    with pytest.raises(scheduler.SchedulerError, match="cannot be rebound"):
        scheduler.install_schedule(
            source_root=source, state_root=state, grant_id="grant-first", backend=backend,
        )


def test_package_initializer_drift_is_checked_before_import(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, _state, _installed = _install(tmp_path, backend)
    (source / "ls/__init__.py").write_text("raise RuntimeError('unverified project import')\n", encoding="utf-8")
    cron_line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    wrapper = subprocess.run(
        shlex.split(cron_line.split(" ", 5)[5]), cwd=tmp_path,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert wrapper.returncode == 78
    assert "unverified project import" not in wrapper.stderr


def test_verified_imports_ignore_valid_bytecode_and_extension_shadows(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, _state, _installed = _install(tmp_path, backend)
    initializer = source / "ls/__init__.py"
    info = initializer.stat()
    cached = Path(importlib.util.cache_from_source(str(initializer)))
    cached.parent.mkdir(exist_ok=True)
    injected = compile("raise RuntimeError('injected bytecode executed')\n", str(initializer), "exec")
    cached.write_bytes(
        importlib.util.MAGIC_NUMBER
        + struct.pack("<III", 0, int(info.st_mtime), info.st_size)
        + marshal.dumps(injected)
    )
    control = subprocess.run(
        [sys.executable, "-B", "-c", "import ls"], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert "injected bytecode executed" in control.stderr
    extension = source / "ls/core/terminal_ops" / (
        "command_service" + importlib.machinery.EXTENSION_SUFFIXES[0]
    )
    extension.write_bytes(b"untrusted native extension shadow")

    cron_line = next(line for line in backend.snapshot.content.splitlines() if line.startswith("* * * * *"))
    wrapper = subprocess.run(
        shlex.split(cron_line.split(" ", 5)[5]), cwd=tmp_path,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert wrapper.returncode == 0, wrapper.stderr
    assert json.loads(wrapper.stdout)["state"] == "completed"
    assert "injected bytecode executed" not in wrapper.stderr


def test_dead_enrolling_owner_is_provenance_not_a_durable_grant_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ls.core.terminal_ops import command_state

    backend = FakeCrontab()
    source, state, installed = _install(tmp_path, backend)
    monkeypatch.setattr(command_state, "owner_liveness", lambda owner: "dead")
    status = scheduler.status_schedule(state_root=state, source_root=source, backend=backend)
    assert status["state"] == "enabled"
    assert status["enabled"] is True
    assert status["enrollment_owner_liveness"] == "dead"

    calls: list[bool] = []
    result = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID,
        source_root=source,
        state_root=state,
        binding_sha256=installed["binding_sha256"],
        gc_callback=lambda: calls.append(True) or {"ok": True},
    )
    assert result["state"] == "completed"
    assert result["dispatched"] is True
    assert calls == [True]


@pytest.mark.parametrize("changed_field", ["host", "uid"])
def test_durable_gc_refuses_a_different_host_or_user(
    tmp_path: Path, changed_field: str,
) -> None:
    backend = FakeCrontab()
    source, state, _installed = _install(tmp_path, backend)
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    record = scheduler._read_record(directory, scheduler.SCHEDULE_ID)
    assert record is not None
    binding = record["binding"]
    owner = binding["authority"]["owner"]
    owner[changed_field] = "other-host" if changed_field == "host" else os.getuid() + 1
    digest = scheduler._binding_digest(binding)
    record["binding_sha256"] = digest
    scheduler._write_record(directory, record)
    remaining, _, _ = scheduler._remove_block(backend.snapshot.content, scheduler.SCHEDULE_ID)
    backend.snapshot = scheduler.CrontabSnapshot(True, scheduler._append_block(
        remaining,
        scheduler._generated_block(
            source, state, scheduler.SCHEDULE_ID, digest,
            binding["source"]["sha256"],
        ),
    ))

    calls: list[bool] = []
    result = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=digest,
        gc_callback=lambda: calls.append(True) or {"ok": True},
    )
    assert result["state"] == "authority_machine_mismatch"
    assert result["dispatched"] is False
    assert calls == []


def test_reboot_pauses_durable_gc_until_explicit_same_host_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ls.core.terminal_ops import command_state

    backend = FakeCrontab()
    source, state, installed = _install(tmp_path, backend)
    monkeypatch.setattr(command_state, "machine_generation", lambda: "same-host-next-boot")

    status = scheduler.status_schedule(state_root=state, source_root=source, backend=backend)
    assert status["state"] == "reboot_rebind_required"
    paused = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=installed["binding_sha256"],
        gc_callback=lambda: pytest.fail("GC must stay paused until rebind"),
    )
    assert paused["state"] == "reboot_rebind_required"
    assert paused["dispatched"] is False

    rebound = scheduler.install_schedule(
        source_root=source, state_root=state, grant_id="grant-first", backend=backend,
    )
    assert rebound["ok"] is True
    assert rebound["state"] == "enabled"
    assert rebound["changed"] is True
    assert scheduler.status_schedule(state_root=state, source_root=source, backend=backend)["state"] == "enabled"


def test_revoke_disables_dispatch_before_exact_crontab_removal(tmp_path: Path) -> None:
    backend = FakeCrontab("# leave this line\n", present=True)
    source, state, installed = _install(tmp_path, backend)
    disabled = scheduler.disable_schedule(state_root=state, backend=backend)
    assert disabled["ok"] is True
    assert disabled["state"] == "disabled"
    assert backend.snapshot.content == "# leave this line\n"
    status = scheduler.status_schedule(state_root=state, source_root=source, backend=backend)
    assert status["state"] == "disabled"
    assert status["enabled"] is False

    calls: list[bool] = []
    run = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID,
        source_root=source,
        state_root=state,
        binding_sha256=installed["binding_sha256"],
        gc_callback=lambda: calls.append(True) or {"ok": True},
    )
    assert run["state"] == "revoked"
    assert run["dispatched"] is False
    assert calls == []
    with pytest.raises(scheduler.SchedulerError, match="revoked recurring grant"):
        scheduler.install_schedule(
            source_root=source, state_root=state, grant_id="grant-first", backend=backend,
        )
    metadata, restored = _read_backup(Path(disabled["backup"]))
    assert metadata["present"] is True
    assert "LOCALSETUP_TERMINAL_GC" in restored


def test_revocation_cannot_interleave_completion_record_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeCrontab()
    source, state, installed = _install(tmp_path, backend)
    original_write = scheduler._write_record
    revocation_attempts: list[str] = []

    def attempt_revoke_at_completion(directory: Path, record: dict) -> None:
        if "last_run" in record and not revocation_attempts:
            try:
                scheduler.disable_schedule(state_root=state, backend=backend)
            except scheduler.SchedulerError as exc:
                revocation_attempts.append(exc.code)
            else:
                revocation_attempts.append("revoked_between_read_and_write")
        original_write(directory, record)

    monkeypatch.setattr(scheduler, "_write_record", attempt_revoke_at_completion)
    result = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=installed["binding_sha256"], gc_callback=lambda: {"ok": True},
    )
    assert result["state"] == "completed"
    assert revocation_attempts == ["busy"]
    disabled = scheduler.disable_schedule(state_root=state, backend=backend)
    assert disabled["state"] == "disabled"
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    record = scheduler._read_record(directory, scheduler.SCHEDULE_ID)
    assert record is not None and record["status"] == "revoked"
    denied = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=installed["binding_sha256"], gc_callback=lambda: pytest.fail("revoked GC dispatched"),
    )
    assert denied["dispatched"] is False


def test_rebind_during_gc_cannot_receive_old_completion(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, state, installed = _install(tmp_path, backend)
    first = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=installed["binding_sha256"], gc_callback=lambda: {"ok": True},
    )
    assert first["state"] == "completed"
    rebound: dict = {}

    def gc_and_rebind() -> dict:
        rebound.update(scheduler.install_schedule(
            source_root=source, state_root=state, grant_id="grant-next", backend=backend,
        ))
        return {"ok": True}

    result = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID, source_root=source, state_root=state,
        binding_sha256=installed["binding_sha256"], gc_callback=gc_and_rebind,
    )
    assert result["state"] == "completed"
    assert rebound["state"] == "enabled"
    assert rebound["binding_sha256"] != installed["binding_sha256"]
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    record = scheduler._read_record(directory, scheduler.SCHEDULE_ID)
    assert record is not None
    assert record["status"] == "enabled"
    assert record["binding_sha256"] == rebound["binding_sha256"]
    assert "last_run" not in record


def test_crontab_timeout_is_reconciled_by_readback_without_replaying_write(tmp_path: Path) -> None:
    backend = FakeCrontab("# keep\n", present=True)
    backend.write_mode = "timeout_after_apply"
    source, state, installed = _install(tmp_path, backend)
    assert installed["ok"] is True
    assert installed["reconciled"] is True
    assert backend.write_count == 1
    assert scheduler.status_schedule(state_root=state, source_root=source, backend=backend)["state"] == "enabled"


def test_uncertain_crontab_result_suppresses_dispatch_and_is_not_replayed(tmp_path: Path) -> None:
    backend = FakeCrontab("# keep\n", present=True)
    backend.write_mode = "unexpected"
    source = _source_fixture(tmp_path)
    state = tmp_path / "private state"
    installed = scheduler.install_schedule(
        source_root=source, state_root=state, grant_id="grant-first", backend=backend,
    )
    assert installed["ok"] is False
    assert installed["state"] == "install_uncertain"
    assert backend.write_count == 1
    calls: list[bool] = []
    run = scheduler.run_bound_schedule(
        scheduler.SCHEDULE_ID,
        source_root=source,
        state_root=state,
        binding_sha256=installed["binding_sha256"],
        gc_callback=lambda: calls.append(True) or {"ok": True},
    )
    assert run["dispatched"] is False
    assert calls == []


def test_nonblocking_overlap_lock_skips_gc(tmp_path: Path) -> None:
    backend = FakeCrontab()
    source, state, installed = _install(tmp_path, backend)
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    lock = directory / "gc-overlap.lock"
    descriptor_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    descriptor = os.fdopen(descriptor_fd, "a+b")
    fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[bool] = []
    try:
        result = scheduler.run_bound_schedule(
            scheduler.SCHEDULE_ID,
            source_root=source,
            state_root=state,
            binding_sha256=installed["binding_sha256"],
            gc_callback=lambda: calls.append(True) or {"ok": True},
        )
    finally:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        descriptor.close()
    assert result["state"] == "overlap_skipped"
    assert result["dispatched"] is False
    assert calls == []


def test_malformed_managed_marker_refuses_mutation_and_keeps_unrelated_content(tmp_path: Path) -> None:
    backend = FakeCrontab(
        "# BEGIN LOCALSETUP_TERMINAL_GC schedule_id=terminal-shell-gc binding=broken\n"
        "* * * * * echo should-not-be-removed\n",
        present=True,
    )
    source = _source_fixture(tmp_path)
    with pytest.raises(scheduler.SchedulerError, match="marker is malformed"):
        scheduler.install_schedule(
            source_root=source, state_root=tmp_path / "private state",
            grant_id="grant-first", backend=backend,
        )
    assert backend.write_count == 0


def test_valid_but_orphaned_managed_entry_is_not_adopted_or_removed(tmp_path: Path) -> None:
    backend = FakeCrontab("# keep\n", present=True)
    source, state, _installed = _install(tmp_path, backend)
    directory = scheduler._schedule_state_dir(state, create=False)
    assert directory is not None
    scheduler._delete_record(directory, scheduler.SCHEDULE_ID)
    before = backend.snapshot.content
    writes = backend.write_count

    with pytest.raises(scheduler.SchedulerError, match="no private owner binding"):
        scheduler.install_schedule(
            source_root=source, state_root=state, grant_id="grant-replacement", backend=backend,
        )
    with pytest.raises(scheduler.SchedulerError, match="no private owner binding"):
        scheduler.disable_schedule(state_root=state, backend=backend)
    assert backend.snapshot.content == before
    assert backend.write_count == writes

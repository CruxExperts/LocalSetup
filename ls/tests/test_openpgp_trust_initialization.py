"""Initial authority publication must not reopen or delete a replacement path."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import stat
from threading import Barrier
from types import SimpleNamespace

import pytest

from ls.core.openpgp import publishing_transition, transition, trust_state
from ls.core.openpgp import trust_initialization as initialization


SCOPE = ("fixture:initialization",)
OWNER, PUBLISHER, RECOVERY = "A" * 40, "B" * 40, "C" * 40


@pytest.fixture
def initial(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "authority.db"
    fingerprints = {b"owner": OWNER, b"publisher": PUBLISHER, b"recovery": RECOVERY}
    monkeypatch.setattr(trust_state, "_clock", lambda: 1_000_000_000)
    monkeypatch.setattr(trust_state, "inspect_key", lambda *, certificate, **_: SimpleNamespace(
        primary_fingerprint=fingerprints[certificate]))
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)

    def create(**overrides):
        arguments = dict(scope=SCOPE, owner_certificate=b"owner",
                         publisher_certificate=b"publisher", recovery_certificates=(b"recovery",))
        arguments.update(overrides)
        return trust_state.initialize_trust_state(path, **arguments)

    return path, create


def test_initial_database_is_serialized_before_destination_creation(initial, monkeypatch):
    path, create = initial
    connect = sqlite3.connect
    opened = []

    def memory_only(database, *args, **kwargs):
        opened.append(database)
        assert database == ":memory:"
        assert not path.exists()
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(trust_state.sqlite3, "connect", memory_only)
    # Existing portable callers must not depend on Linux held-fd publication.
    monkeypatch.setattr(initialization.ExclusiveOutput, "publish",
                        lambda *_: pytest.fail("Linux-only publisher called"))
    result = create()
    assert opened == [":memory:"]
    assert (result.revision, result.epoch) == (1, 1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes()[18:20] == bytes((1, 1))
    with connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert dict(db.execute("SELECT fingerprint,certificate FROM keys")) == {
            OWNER: b"owner", PUBLISHER: b"publisher"}
        assert db.execute("SELECT fingerprint,certificate FROM recovery_keys").fetchall() == [
            (RECOVERY, b"recovery")]
        db.execute("INSERT INTO consumed VALUES (?,?)", ("fixture", "test"))
    assert list(path.parent.iterdir()) == [path]


def test_initialization_handles_short_writes(initial, monkeypatch):
    path, create = initial
    write = os.write
    monkeypatch.setattr(initialization.os, "write", lambda fd, data: write(fd, data[:137]))
    result = create()
    assert trust_state.load_trust_state(path, expected_scope=SCOPE) == result


@pytest.mark.parametrize("failure", ["write", "zero_write", "file_sync", "directory_sync"])
def test_initialization_failure_removes_only_created_store(initial, monkeypatch, failure):
    path, create = initial
    fsync = os.fsync

    def fail(*_):
        raise OSError("injected failure")

    if failure == "write":
        monkeypatch.setattr(initialization.os, "write", fail)
    elif failure == "zero_write":
        monkeypatch.setattr(initialization.os, "write", lambda *_: 0)
    else:
        def sync(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if directory == (failure == "directory_sync"):
                fail()
            fsync(fd)
        monkeypatch.setattr(initialization.os, "fsync", sync)
    with pytest.raises(trust_state.TrustStateError, match="cannot be created"):
        create()
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_failure_preserves_replacement_store(initial, monkeypatch, fail_after_replace):
    path, create = initial

    def replace_and_fail(_fd, _data):
        path.unlink()
        path.write_bytes(b"replacement")
        if fail_after_replace:
            raise OSError("write interrupted after replacement")

    monkeypatch.setattr(initialization, "_write_database", replace_and_fail)
    with pytest.raises(trust_state.TrustStateError):
        create()
    assert path.read_bytes() == b"replacement"


def test_parent_symlink_race_is_a_trust_state_error(initial, monkeypatch):
    path, create = initial
    serialize = trust_state._initial_database
    moved = path.parent.with_name("moved")

    def replace_after_validation(*args):
        result = serialize(*args)
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
        return result

    monkeypatch.setattr(trust_state, "_initial_database", replace_after_validation)
    with pytest.raises(trust_state.TrustStateError):
        create()
    assert list(moved.iterdir()) == []


def test_parent_replacement_is_rejected_without_deleting_new_store(initial, monkeypatch):
    path, create = initial
    original = initialization._write_database
    moved = path.parent.with_name("moved")

    def replace_parent(fd, data):
        original(fd, data)
        path.parent.rename(moved)
        path.parent.mkdir(mode=0o700)
        path.write_bytes(b"replacement parent")

    monkeypatch.setattr(initialization, "_write_database", replace_parent)
    with pytest.raises(trust_state.TrustStateError):
        create()
    assert path.read_bytes() == b"replacement parent"
    assert list(moved.iterdir()) == []


def test_concurrent_initialization_has_one_winner(initial, monkeypatch):
    path, create = initial
    publish = initialization.publish_initial_database
    barrier = Barrier(2)

    def together(*args):
        barrier.wait(timeout=5)
        return publish(*args)

    monkeypatch.setattr(initialization, "publish_initial_database", together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except trust_state.TrustStateError:
                outcomes.append(None)
    winners = [value for value in outcomes if value is not None]
    assert len(winners) == 1
    assert trust_state.load_trust_state(path, expected_scope=SCOPE) == winners[0]


def test_missing_serializer_fails_before_creating_store(initial, monkeypatch):
    path, create = initial
    monkeypatch.setattr(trust_state.sqlite3, "Connection", object)
    with pytest.raises(trust_state.TrustStateError, match="serialization unavailable"):
        create()
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("field", ["owner_certificate", "publisher_certificate", "recovery_certificates"])
def test_certificate_size_is_bounded_before_inspection(initial, monkeypatch, field):
    path, create = initial
    monkeypatch.setattr(trust_state, "inspect_key", lambda **_: pytest.fail("unbounded inspection"))
    oversized = b"x" * (trust_state._MAX_CERTIFICATE_BYTES + 1)
    value = (oversized,) if field == "recovery_certificates" else oversized
    with pytest.raises(trust_state.TrustStateError):
        create(**{field: value})
    assert list(path.parent.iterdir()) == []

import errno
import fcntl
import json
from pathlib import Path

import pytest

from ls.core.apply import apply_plan
from ls.core.locking import package_root_lock, package_root_lock_path, PackageRootLockTimeout
from ls.core.paths import global_layout
from ls.core.plan import build_install_plan
from ls.core.rollback import rollback
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EAGAIN])
def test_package_root_lock_propagates_body_lock_errors_once(tmp_path, monkeypatch, error_number):
    import ls.core.locking as locking

    original_flock = fcntl.flock
    flock_calls = []

    def recording_flock(fd, operation):
        flock_calls.append(operation)
        return original_flock(fd, operation)

    monkeypatch.setattr(locking.fcntl, "flock", recording_flock)
    home = tmp_path / "home"
    error = OSError(error_number, "body failed")
    body_calls = []

    with pytest.raises(OSError) as raised:
        with package_root_lock(home, timeout=0):
            body_calls.append(None)
            raise error

    assert raised.value is error
    assert len(body_calls) == 1
    assert flock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN]

    flock_calls.clear()
    with package_root_lock(home, timeout=0) as lock:
        assert lock["acquired"] is True
    assert flock_calls == [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN]


def test_package_root_lock_releases_after_metadata_failure(tmp_path, monkeypatch):
    home = tmp_path / "home"
    lock_path = package_root_lock_path(home)
    original_open = Path.open
    metadata_error = OSError(errno.EIO, "lock metadata write failed")

    class FailingWrite:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, _value):
            raise metadata_error

    def failing_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == lock_path:
            return FailingWrite(handle)
        return handle

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", failing_open)
        with pytest.raises(OSError) as raised:
            with package_root_lock(home, timeout=0):
                pytest.fail("body must not run when metadata writing fails")

    assert raised.value is metadata_error
    with package_root_lock(home, timeout=0) as lock:
        assert lock["acquired"] is True


def test_rollback_waits_for_shared_lock_and_prevalidates_all_paths(tmp_path, monkeypatch):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor']), home)
    receipt = root / '.localsetup/lock.json';original = receipt.read_bytes()
    lock = json.loads(original);package = Path(lock['installed_skills'][0])
    registry = Path(lock['registry_path']);before = registry.read_bytes()
    monkeypatch.setenv('LOCALSETUP_PACKAGE_ROOT_LOCK_TIMEOUT', '0')
    with package_root_lock(global_layout(home).localsetup_home):
        with pytest.raises(PackageRootLockTimeout):rollback(root, home)
    assert receipt.read_bytes() == original and registry.read_bytes() == before and package.exists()
    for field, invalid in [('installed_skills', str(tmp_path / 'outside-package')),
                           ('installed_skills', str(package.parent)),
                           ('adapter_state', str(tmp_path / 'outside-adapter'))]:
        malformed = json.loads(original);malformed[field].append(invalid)
        receipt.write_text(json.dumps(malformed))
        with pytest.raises(RuntimeError, match='refusing to rollback'):
            rollback(root, home)
        assert package.exists() and registry.read_bytes() == before
    receipt.write_bytes(original)

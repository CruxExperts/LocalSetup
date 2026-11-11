import json
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-name contract")
def test_native_process_name_tracks_version_without_changing_arguments():
    code = """import json,sys
from pathlib import Path
from ls.core import process_identity
process_identity.framework_version = lambda: '9.8.7'
before = Path('/proc/self/cmdline').read_bytes()
assert process_identity.set_process_name()
print(json.dumps([Path('/proc/self/comm').read_text().strip(), before == Path('/proc/self/cmdline').read_bytes()]))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["LS-v9.8.7", True]


def test_format_context_version_index_and_safe_components():
    from ls.core.process_identity import NameContext, NameFormat, format_name, next_available_index

    context = NameContext("reporter", "demo", "1.2.3", True)
    assert format_name(context) == "reporter-demo-v1.2.3"
    assert format_name(NameContext("reporter", version="3.0", version_verified=False)) == "reporter"
    assert format_name(NameContext("reporter", index=2)) == "reporter-2"
    assert format_name(context, NameFormat(include_scope=False, include_version=False)) == "reporter"
    assert next_available_index(context, ["reporter-demo-v1.2.3", "reporter-demo-v1.2.3-1"]) == 2
    with pytest.raises(ValueError):
        format_name(NameContext("/private/path"))
    with pytest.raises(ValueError):
        format_name(NameContext("app", index=-1))


def test_bounded_name_keeps_instance_suffix():
    from ls.core.process_identity import NameContext, bounded_name

    assert bounded_name(NameContext("reporting-worker", index=12), 15) == "reporting-wo-12"


def test_unsupported_platform_reports_unmodified(monkeypatch):
    from ls.core import process_identity

    monkeypatch.setattr(process_identity.sys, "platform", "other")
    result = process_identity.apply_process_name(process_identity.NameContext("reporter"))
    assert result.status == "unsupported"
    assert result.observed_name is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-name contract")
def test_process_name_readback_and_arguments():
    code = '''import json
from pathlib import Path
from ls.core.process_identity import NameContext, apply_process_name
before = Path('/proc/self/cmdline').read_bytes()
result = apply_process_name(NameContext('reporting-worker', scope='queue', index=1))
print(json.dumps([result.status, result.full_name, result.observed_name, before == Path('/proc/self/cmdline').read_bytes()]))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["truncated", "reporting-worker-queue-1", "reporting-wor-1", True]


def test_process_name_readback_mismatch_is_not_success(monkeypatch):
    from ls.core import process_identity

    class Comm:
        def write_text(self, *_args, **_kwargs):
            return 1

        def read_text(self, **_kwargs):
            return "different\n"

    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "Path", lambda _path: Comm())
    result = process_identity.apply_process_name(process_identity.NameContext("reporter"))
    assert result.status == "verification_failed"
    assert result.observed_name == "different"


def test_process_name_permission_failure_is_explicit(monkeypatch):
    from ls.core import process_identity

    class Comm:
        def write_text(self, *_args, **_kwargs):
            raise PermissionError("denied")

    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "Path", lambda _path: Comm())
    result = process_identity.apply_process_name(process_identity.NameContext("reporter"))
    assert result.status == "unavailable"
    assert result.detail == "PermissionError"


def test_owned_process_inventory_selects_suffix_and_preserves_existing(monkeypatch, tmp_path):
    from ls.core import process_identity

    for pid, name in ((111, "worker"), (222, "worker-1"), (333, "python")):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "comm").write_text(name + "\n", encoding="ascii")
    scandir = process_identity.os.scandir
    monkeypatch.setattr(process_identity.os, "scandir", lambda _path: scandir(tmp_path))
    monkeypatch.setattr(process_identity.os, "getpid", lambda: 333)
    context = process_identity.NameContext("worker")
    assert process_identity._owned_process_context(context).index == 2
    (tmp_path / "333" / "comm").write_text("worker-3\n", encoding="ascii")
    assert process_identity._owned_process_context(context).index == 3


def test_verify_native_name_reports_missing_or_mismatched(monkeypatch):
    from ls.core import process_identity

    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    class Comm:
        def read_text(self, **_kwargs):
            return "different\n"
    monkeypatch.setattr(process_identity, "Path", lambda *_: Comm())
    result = process_identity.verify_process_name(process_identity.NameContext("worker"), 42)
    assert result.status == "verification_failed"
    assert result.observed_name == "different"
    class Missing:
        def read_text(self, **_kwargs):
            raise FileNotFoundError()
    monkeypatch.setattr(process_identity, "Path", lambda *_: Missing())
    assert process_identity.verify_process_name(process_identity.NameContext("worker"), 42).status == "unavailable"
    with pytest.raises(ValueError):
        process_identity.verify_process_name(process_identity.NameContext("worker"), 0)


def test_owned_inventory_failure_does_not_apply_name(monkeypatch):
    from ls.core import process_identity

    monkeypatch.setattr(process_identity.sys, "platform", "linux")
    monkeypatch.setattr(process_identity, "_owned_process_context", lambda _context: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(process_identity, "apply_process_name", lambda *_: pytest.fail("must not apply"))
    assert process_identity.set_process_name() is False

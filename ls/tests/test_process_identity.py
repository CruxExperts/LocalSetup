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

    assert bounded_name(NameContext("reporting-worker", index=12), 15) == "reporting-w.-12"


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
    assert json.loads(result.stdout) == ["truncated", "reporting-worker-queue-1", "report.-queue-1", True]


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process inspection")
def test_resolve_real_child_exec_and_distinct_dispatch():
    import os
    from pathlib import Path
    from ls.core.process_identity import NameContext, resolve_workload

    child = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"])
    try:
        import time
        for _ in range(100):
            if Path(f"/proc/{child.pid}/exe").resolve().name == "sleep":
                break
            time.sleep(0.01)
        result = resolve_workload(NameContext("reporter"), pid=child.pid,
                                  command_path=("reporter", "synchronize"))
        assert result.pid == child.pid
        assert result.application is None
        assert result.caller_application == "reporter"
        assert result.application_source == "unresolved"
        assert result.worker == "sleep"
        assert result.launcher == Path(os.readlink("/proc/self/exe")).name
        assert result.ancestry[-2:] == (result.launcher, "sleep")
        assert result.command_path == ("reporter", "synchronize")
        assert result.compact_path == "reporter/synchro~"
        assert result.handoff == "different_ancestor_exec_unresolved"
        assert "sleep 30" not in repr(result)
        corroborated = resolve_workload(NameContext("sleep"), pid=child.pid)
        assert corroborated.application == "sleep"
        assert corroborated.application_source == "executable"
        assert corroborated.compact_path is None
    finally:
        child.terminate()
        child.wait()


def test_resolver_distinguishes_caller_assertion_from_independent_version(monkeypatch):
    from ls.core import process_identity as identity

    monkeypatch.setattr(identity, "framework_version", lambda: "2.3.4")
    claim = identity.NameContext("app", version="2.3.4", version_verified=True)
    unverified = identity.resolve_workload(claim)
    assert unverified.context.version == "2.3.4"
    assert unverified.context.version_verified
    assert unverified.version_source == "caller_asserted"
    verified = identity.resolve_workload(claim, owned_runtime=True)
    assert verified.context.version == "2.3.4"
    assert verified.context.version_verified
    assert verified.version_source == "localsetup_package"
    mismatch = identity.resolve_workload(identity.NameContext("app", version="1.0"), owned_runtime=True)
    assert mismatch.version_source == "omitted"


def test_resolver_reports_unsupported_and_unresolvable(monkeypatch):
    from ls.core import process_identity as identity

    monkeypatch.setattr(identity.sys, "platform", "other")
    unsupported = identity.resolve_workload(identity.NameContext("app"), pid=123)
    assert unsupported.status == "unsupported"
    assert unsupported.pid == 123
    monkeypatch.setattr(identity.sys, "platform", "linux")
    monkeypatch.setattr(identity.os, "readlink", lambda *_: (_ for _ in ()).throw(FileNotFoundError()))
    unresolvable = identity.resolve_workload(identity.NameContext("app"), pid=123)
    assert unresolvable.status == "unresolvable"
    assert unresolvable.pid == 123


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process inspection")
def test_apply_and_verify_preserve_caller_verified_versions():
    code = '''import json, os
from ls.core import process_identity as p
original = p.resolve_workload
calls = []
def resolve(*args, **kwargs):
    calls.append(kwargs.get("pid"))
    return original(*args, **kwargs)
p.resolve_workload = resolve
context = p.NameContext("worker", version="9.9", version_verified=True)
applied = p.apply_process_name(context)
verified = p.verify_process_name(context, os.getpid())
print(json.dumps([applied.full_name, verified.status, calls == [os.getpid(), os.getpid()]]))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["worker-v9.9", "verified", True]


def test_declared_path_retains_application_and_resolution_explains_compaction():
    from ls.core.process_identity import NameContext, bounded_name, format_name, resolve_workload

    path = ("myrig", "library", "documents", "archive", "check")
    context = NameContext("owner", scope="batch", index=2, command_path=path)
    result = resolve_workload(context)
    assert result.context.command_path == path
    assert result.full_name == format_name(context) == "owner-myrig-library-documents-archive-check-batch-2"
    assert result.compact_name == bounded_name(context, 15)
    assert len(result.compact_name) <= 15
    assert result.compact_name.endswith("-2")
    assert result.compact_name.startswith("o.")
    assert "c." in result.compact_name
    assert "-.-" in result.compact_name
    assert result.omitted_components
    assert all(original.startswith(abbreviation[:-1]) and abbreviation.endswith(".")
               for original, abbreviation in result.abbreviations)
    explicit = resolve_workload(NameContext("owner"), command_path=path)
    assert explicit.context.command_path == explicit.command_path == path
    assert explicit.full_name == "owner-myrig-library-documents-archive-check"


def test_application_distinguishes_shared_dispatch_without_duplicate_entrypoint():
    from ls.core.process_identity import NameContext, bounded_name, format_name, resolve_workload

    path = ("lib", "check")
    first = NameContext("one", command_path=path)
    second = NameContext("two", command_path=path)
    assert format_name(first) == "one-lib-check"
    assert format_name(second) == "two-lib-check"
    assert bounded_name(first, 15) != bounded_name(second, 15)
    assert resolve_workload(first).full_name != resolve_workload(second).full_name
    assert format_name(NameContext("one", command_path=("one", *path))) == "one-lib-check"


def test_resolver_metadata_uses_selected_format_options():
    from ls.core.process_identity import NameContext, NameFormat, bounded_name, format_name, resolve_workload

    context = NameContext("reporter", scope="batch", version="1.2", version_verified=True,
                          index=2, command_path=("library", "check"))
    options = NameFormat(include_scope=False, include_version=False)
    result = resolve_workload(context, options=options)
    assert result.full_name == format_name(context, options) == "reporter-library-check-2"
    assert result.compact_name == bounded_name(context, 15, options)
    assert result.compact_name != bounded_name(context, 15)
    assert all(original not in ("batch", "v1.2") for original, _ in result.abbreviations)
    assert "batch" not in result.omitted_components
    assert "v1.2" not in result.omitted_components
    assert result.pid is None


def test_unrepresentable_native_name_reports_failure():
    from ls.core.process_identity import NameContext, bounded_name, resolve_workload

    context = NameContext("app", index=123456789012345, command_path=("app", "task"))
    result = resolve_workload(context)
    assert result.compact_name is None
    assert "cannot represent" in result.compact_detail
    with pytest.raises(ValueError, match="cannot represent"):
        bounded_name(context, 15)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process naming")
def test_nested_name_applies_and_verifies_without_changing_argv():
    code = '''import json, os
from pathlib import Path
from ls.core.process_identity import NameContext, apply_process_name, bounded_name, verify_process_name
context = NameContext("app", index=2, command_path=("application", "library", "documents", "check"))
before = Path("/proc/self/cmdline").read_bytes()
applied = apply_process_name(context)
verified = verify_process_name(context, os.getpid())
print(json.dumps([applied.status, applied.observed_name, bounded_name(context, 15), verified.status,
                  before == Path("/proc/self/cmdline").read_bytes()]))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    status, observed, expected, verified, unchanged = json.loads(result.stdout)
    assert status == verified == "truncated"
    assert observed == expected and expected.endswith("-2") and unchanged


def test_nested_inventory_allocates_and_preserves_instance(monkeypatch, tmp_path):
    from ls.core import process_identity as identity

    context = identity.NameContext("app", command_path=("application", "documents", "check"))
    for pid, name in ((111, identity.bounded_name(context, 15)),
                      (222, identity.bounded_name(identity.replace(context, index=1), 15)),
                      (333, "python")):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "comm").write_text(name + "\n", encoding="ascii")
    scandir = identity.os.scandir
    monkeypatch.setattr(identity.os, "scandir", lambda _: scandir(tmp_path))
    monkeypatch.setattr(identity.os, "getpid", lambda: 333)
    assert identity._owned_process_context(context).index == 2
    existing = identity.replace(context, index=2)
    (tmp_path / "333" / "comm").write_text(identity.bounded_name(existing, 15) + "\n", encoding="ascii")
    assert identity._owned_process_context(context).index == 2
    assert identity.next_available_index(context, [identity.format_name(context)]) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process naming")
def test_parent_verifies_cooperating_child_native_name():
    from ls.core.process_identity import NameContext, bounded_name, verify_process_name

    context = NameContext("app", index=2, command_path=("application", "library", "documents", "check"))
    code = '''import sys
from ls.core.process_identity import NameContext, apply_process_name
context = NameContext("app", index=2, command_path=("application", "library", "documents", "check"))
result = apply_process_name(context)
print(result.status, flush=True)
sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "truncated"
        result = verify_process_name(context, child.pid)
        assert result.status == "truncated"
        assert result.observed_name == bounded_name(context, 15)
    finally:
        child.communicate(input="\n", timeout=10)

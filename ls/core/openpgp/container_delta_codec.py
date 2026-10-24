"""Bounded optional zstd patch codec; callers supply authenticated file evidence.

This module grants no trust, source discovery, publication or key authority.
"""
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

from .container_delta_records import FileEvidence
from .envelope_io import AnchoredFile, ExclusiveOutput


class DeltaCodecError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DeltaCodec:
    executable: Path
    sha256: str
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 30
    timeout_seconds: float = 60
    max_input_bytes: int = 256 * 1024 * 1024
    max_output_bytes: int = 256 * 1024 * 1024
    max_diagnostics_bytes: int = 64 * 1024

    def __post_init__(self):
        FileEvidence(0, self.sha256)
        if (not isinstance(self.executable, Path) or not self.executable.is_absolute()
                or '..' in self.executable.parts
                or any(type(value) is not int or value <= 0 for value in (
                    self.memory_bytes, self.cpu_seconds, self.max_input_bytes,
                    self.max_output_bytes, self.max_diagnostics_bytes))
                or not 64*1024*1024 <= self.memory_bytes <= 16*1024**3
                or self.cpu_seconds > 3600 or self.max_diagnostics_bytes > 1024*1024
                or type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 3600):
            raise ValueError("Invalid delta codec policy")


def _evidence(fd, maximum, check):
    length, digest = 0, hashlib.sha256()
    while True:
        check()
        data = os.pread(fd, min(65536, maximum-length+1), length)
        if not data:
            return FileEvidence(length, digest.hexdigest())
        length += len(data)
        if length > maximum:
            raise DeltaCodecError('limit_exceeded')
        digest.update(data)


def _verified(opened, expected, policy, check):
    if (not isinstance(expected, FileEvidence) or opened.size > policy.max_input_bytes
            or opened.size != expected.length or not opened.unchanged()
            or _evidence(opened.file, policy.max_input_bytes, check) != expected
            or not opened.unchanged()):
        raise DeltaCodecError('input_changed')


# A separate isolated interpreter applies resource limits before exec. Avoid
# preexec_fn in a potentially multithreaded broker, and inherit no secrets.
_LAUNCHER = (
    "import os,resource,signal,sys;"
    "signal.signal(signal.SIGXFSZ,signal.SIG_DFL);"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
    "m,c,f=map(int,sys.argv[1:4]);"
    "resource.setrlimit(resource.RLIMIT_AS,(m,m));"
    "resource.setrlimit(resource.RLIMIT_CPU,(c,c));"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(f,f));"
    "os.execve(int(sys.argv[4]),sys.argv[5:],dict(os.environ))"
)


def _run(policy, base, payload, output_fd, *, decode, output_limit, check):
    if sys.platform != 'linux' or not hasattr(os, 'O_NOFOLLOW'):
        raise DeltaCodecError('unsupported')
    descriptor = os.open(policy.executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    process = None
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.geteuid())
                or before.st_mode & 0o022 or not before.st_mode & 0o111
                or _evidence(descriptor, 32*1024*1024, check).sha256 != policy.sha256):
            raise DeltaCodecError('codec_changed')
        arguments = [str(policy.executable), '-q', '-f', '--single-thread', '-c',
                     '-d' if decode else '-3', '--patch-from=/proc/self/fd/'+str(base.file),
                     '--', '/proc/self/fd/'+str(payload.file)]
        command = [sys.executable, '-I', '-B', '-c', _LAUNCHER,
                   str(policy.memory_bytes), str(policy.cpu_seconds), str(max(1, output_limit)),
                   str(descriptor), *arguments]
        check()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output_fd,
            stderr=subprocess.PIPE, pass_fds=(descriptor, base.file, payload.file),
            env={'LC_ALL': 'C', 'PATH': os.defpath}, start_new_session=True)
        diagnostics = 0
        status = None
        assert process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map() or status is None:
                check()
                # Keep the leader unreaped until group cleanup; its reserved
                # PID prevents signalling a reused, unrelated process group.
                status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                for key, _ in selector.select(.05):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    diagnostics += len(data)
                    if diagnostics > policy.max_diagnostics_bytes:
                        raise DeltaCodecError('diagnostics_limit')
        if status.si_code != os.CLD_EXITED or status.si_status != 0:
            raise DeltaCodecError('codec_failed')
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DeltaCodecError('codec_changed')
    finally:
        if process is not None:
            # Also remove any descendants from the owned process group, even
            # if a faulty codec exits before closing an inherited descriptor.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if process.stderr is not None:
                process.stderr.close()
        os.close(descriptor)


def _transform(base_path, payload_path, output_path, *, base_evidence, payload_evidence,
               expected_result, policy, budget, decode):
    deadline = min(budget.deadline, time.monotonic()+policy.timeout_seconds)
    def check():
        budget.checkpoint()
        if time.monotonic() >= deadline:
            raise DeltaCodecError('deadline_exceeded')
    limit = policy.max_output_bytes
    if expected_result is not None:
        if not isinstance(expected_result, FileEvidence) or expected_result.length > limit:
            raise DeltaCodecError('limit_exceeded')
        limit = expected_result.length
    budget.reserve_temp(limit)
    retained = 0
    try:
        with AnchoredFile(base_path, private=True) as base, AnchoredFile(payload_path, private=True) as payload, \
                ExclusiveOutput(output_path) as output:
            _verified(base, base_evidence, policy, check)
            _verified(payload, payload_evidence, policy, check)
            name, fd = output.stage()
            try:
                _run(policy, base, payload, fd, decode=decode, output_limit=limit, check=check)
                os.fsync(fd)
                observed = _evidence(fd, limit, check)
                if expected_result is not None and observed != expected_result:
                    raise DeltaCodecError('result_mismatch')
                if not base.unchanged() or not payload.unchanged():
                    raise DeltaCodecError('input_changed')
                # An uneconomic patch is a publication fallback, never a read
                # fallback or authority to create a new snapshot.
                if not decode and observed.length >= payload_evidence.length:
                    return None
                budget.retain(observed.length)
                check()
                if not base.unchanged() or not payload.unchanged():
                    raise DeltaCodecError('input_changed')
                output.publish(name)
                check()
                if not base.unchanged() or not payload.unchanged():
                    raise DeltaCodecError('input_changed')
                output.published_identity = None
                retained = observed.length
                return observed
            finally:
                os.close(fd)
    except DeltaCodecError:
        raise
    except Exception:
        raise DeltaCodecError('unavailable') from None
    finally:
        budget.release_temp(limit-retained)


def create_patch(base_path, result_path, output_path, *, base, result, policy: DeltaCodec, budget):
    return _transform(base_path, result_path, output_path, base_evidence=base, payload_evidence=result,
                      expected_result=None, policy=policy, budget=budget, decode=False)


def apply_patch(base_path, patch_path, output_path, *, base, patch, result, policy: DeltaCodec, budget):
    return _transform(base_path, patch_path, output_path, base_evidence=base, payload_evidence=patch,
                      expected_result=result, policy=policy, budget=budget, decode=True)

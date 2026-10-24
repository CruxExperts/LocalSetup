from __future__ import annotations

import fcntl
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import pytest

from ls.core.openpgp import envelope as envelope_api
from ls.core.openpgp.envelope import EnvelopeError, EnvelopeErrorCode
from ls.core.openpgp.passphrase_pipe import PassphrasePipe


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


def test_large_passphrase_drains_after_child_spawn_without_full_pipe_deadlock(tmp_path: Path):
    marker = tmp_path / "child-started"
    secret = bytearray(b"p" * 4096)
    pipe = PassphrasePipe(secret)
    read_fd, write_fd = pipe.read_fd, pipe.write_fd
    try:
        fcntl.fcntl(read_fd, fcntl.F_SETPIPE_SZ, 4096)
    except OSError:
        pipe.close()
        pytest.skip("the host does not permit reducing a disposable pipe capacity")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; fd=int(sys.argv[1]); open(sys.argv[2],'w').close(); "
            "time.sleep(.05); data=bytearray(); "
            "\nwhile True:\n chunk=os.read(fd,8192)\n if not chunk: break\n data.extend(chunk)\n"
            "print(len(data), data[-1:] == b'\\n')",
            str(read_fd), str(marker),
        ],
        pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    pipe.close_reader()
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert marker.exists(), "reader child did not start before the parent began writing"
        with selectors.DefaultSelector() as selector:
            selector.register(write_fd, selectors.EVENT_WRITE)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "bounded pipe writer stalled"
                events = selector.select(remaining)
                assert events, "bounded pipe writer stalled"
                for key, _mask in events:
                    if pipe.write_some():
                        selector.unregister(key.fileobj)
                        pipe.close_writer()
        stdout, stderr = child.communicate(timeout=3)
        assert child.returncode == 0, "reader child failed safely"
        assert stdout.strip() == "4097 True"
        assert stderr == ""
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        pipe.close()
    assert not any(secret)
    assert pipe.read_fd == pipe.write_fd == -1
    _assert_closed(read_fd)
    _assert_closed(write_fd)


def test_early_child_exit_closes_pipe_and_wipes_buffer():
    secret = bytearray(b"short-secret")
    pipe = PassphrasePipe(secret)
    read_fd, write_fd = pipe.read_fd, pipe.write_fd
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"], pass_fds=(read_fd,),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pipe.close_reader()
    try:
        assert child.wait(timeout=3) == 0
        with pytest.raises(BrokenPipeError):
            pipe.write_some()
    finally:
        pipe.close()
    assert not any(secret)
    _assert_closed(read_fd)
    _assert_closed(write_fd)


def test_gpg_spawn_failure_closes_pipe_descriptors_and_wipes_secret(monkeypatch, tmp_path):
    created: list[PassphrasePipe] = []
    original_pipe = envelope_api.PassphrasePipe

    class TrackingPipe(original_pipe):
        def __init__(self, secret):
            super().__init__(secret)
            created.append(self)

    secret = bytearray(b"spawn-failure-secret")
    monkeypatch.setattr(envelope_api, "PassphrasePipe", TrackingPipe)

    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(envelope_api.subprocess, "Popen", unavailable)
    with pytest.raises(EnvelopeError) as failure:
        envelope_api._run_gpg(
            "gpg", tmp_path,
            ("--pinentry-mode", "loopback", "--passphrase-fd", "{passphrase_fd}"),
            stdin_data=b"synthetic operation",
            passphrase=secret,
            failure_code=EnvelopeErrorCode.SIGN_ENCRYPT_FAILED,
        )

    assert failure.value.code is EnvelopeErrorCode.SIGN_ENCRYPT_FAILED
    assert len(created) == 1
    assert not any(secret)
    assert created[0].read_fd == created[0].write_fd == -1

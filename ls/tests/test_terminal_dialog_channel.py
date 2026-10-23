"""Private dialog transport with actual helper processes and isolated PTYs."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import socket
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

from ls.core.terminal_ops.command_state import process_info
from ls.core.terminal_ops.dialog_channel import (
    ChannelClosed, DialogChannel, HelperIdentity, MAX_FRAME, decode_response,
    encode_response, receive_frame,
)
from ls.core.terminal_ops.dialog_model import Choice, Field, Form, Response


FORM = Form("Private channel probe", (
    Field("secret", "Password", kind="password", required=True),
    Field("roles", "Roles", kind="checkbox", choices=(Choice("read", "Read"), Choice("write", "Write"))),
    Field("target", "Target", kind="radio", required=True, choices=(Choice("first", "First"), Choice("second", "Second"))),
))


def test_private_wire_bounds_and_schema_validation() -> None:
    response = Response({"secret": bytearray(b"SyntheticOnly"), "roles": ("write", "read"), "target": ("second",)})
    payload = encode_response(FORM, response)
    parsed = decode_response(FORM, payload)
    assert parsed.values == {"secret": b"SyntheticOnly", "roles": ("read", "write"), "target": ("second",)}
    parsed.dispose()
    for damaged in (payload[:-1], payload + b"trailing", b"S" + struct.pack("!I", 4097),
                    b"S" + struct.pack("!I", 1) + b"\x00" + struct.pack("!I", 0)):
        with pytest.raises(ChannelClosed):
            decode_response(FORM, bytearray(damaged))
    response.dispose()
    payload[:] = b"\0" * len(payload)
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(ChannelClosed, match="bounds"):
            receive_frame(receiver, lambda: True)
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("mode", ["delivered", "unknown", "lost_ack", "cancel", "owner_exit", "expired", "wrong_generation", "job_control_change"])
def test_real_helper_private_delivery_is_single_use_and_bound(mode: str, monkeypatch) -> None:
    owner = process_info(os.getpid())
    assert owner is not None
    delivered = []
    owner_live = [True]
    if mode == "job_control_change":
        from ls.core.terminal_ops import dialog_channel

        observations = [0]

        def changing_job_control(pid):
            info = process_info(pid)
            observations[0] += 1
            return info | {"tpgid": observations[0]} if info is not None else None

        monkeypatch.setattr(dialog_channel, "process_info", changing_job_control)

    def consume(response: Response) -> bool:
        assert response.values == {"secret": b"ChannelCanary7", "roles": ("write",), "target": ("second",)}
        delivered.append(True)
        if mode == "unknown":
            raise OSError("consumer acknowledgement unavailable")
        if mode == "lost_ack":
            channel._connection.shutdown(socket.SHUT_RDWR)
        return True

    deadline = time.monotonic() + (1 if mode in {"expired", "wrong_generation"} else 12)
    channel = DialogChannel(FORM, request_id=uuid.uuid4().hex, deadline=deadline,
                            validate_current=lambda: owner_live[0], deliver=consume)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = None
    output = bytearray()

    def read_until(marker: bytes, timeout: float = 4) -> None:
        until = time.monotonic() + timeout
        while marker not in output and time.monotonic() < until:
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
        assert marker in output, bytes(output)[-2000:]

    try:
        channel.open()
        assert Path(channel.path).stat().st_mode & 0o777 == 0o600
        assert Path(channel.path).parent.stat().st_mode & 0o777 == 0o700
        # An unrelated same-UID connector cannot claim the outstanding request.
        with socket.socket(socket.AF_UNIX) as unqualified:
            unqualified.connect(channel.path)
        argv = [sys.executable, "-m", "ls.core.terminal_ops.dialog_helper", "--socket", channel.path,
                "--request-id", channel.request_id, "--controller-pid", str(owner["pid"]),
                "--controller-starttime", str(owner["starttime"]), "--deadline", str(deadline)]
        assert all("ChannelCanary7" not in part for part in argv)
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                   start_new_session=True, env={**os.environ, "TERM": "xterm-256color"})
        channel.admit(HelperIdentity(tuple(argv), sys.executable, owner["pid"],
                                     owner["starttime"] + (1 if mode == "wrong_generation" else 0)))
        if mode == "wrong_generation":
            assert process.wait(timeout=4) != 0
            assert not delivered
            return
        read_until(b"[hidden]")
        assert channel.peer_identity is not None and channel.peer_identity["pid"] == process.pid
        os.write(master, b"ChannelCanary7")
        if mode == "cancel":
            os.write(master, b"\x03")
        elif mode == "owner_exit":
            owner_live[0] = False
        elif mode != "expired":
            # Select the second checkbox using ncurses application cursor mode.
            os.write(master, b"\t\x1bOB \t\x1bOB \x13")
        process.wait(timeout=4)
        while select.select([master], [], [], 0.05)[0]:
            output.extend(os.read(master, 65536))
        if channel._thread is not None:
            channel._thread.join(timeout=1)
        if mode in {"delivered", "lost_ack", "job_control_change"}:
            assert channel.status == "delivered" and delivered == [True]
        elif mode == "unknown":
            assert channel.status == "delivery_unknown" and delivered == [True]
        else:
            expected = {"cancel": "cancelled", "owner_exit": "unavailable", "expired": "expired"}[mode]
            assert channel.status == expected and delivered == []
        assert b"ChannelCanary7" not in output
        assert list(Path(channel.path).parent.iterdir()) == [Path(channel.path)]
        with pytest.raises(ChannelClosed):
            channel.admit(HelperIdentity(tuple(argv), sys.executable, owner["pid"], owner["starttime"]))
    finally:
        channel.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        channel.close()
        assert not channel.cleanup_pending
        assert not Path(channel.path).exists()
        os.close(master)
        os.close(slave)

"""Private, authentication-only candidate delivery for remote sudo."""
from __future__ import annotations

import subprocess
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from .credentials import child_environment

FIFO_WRITER = r'''import os,stat,sys
nonce=sys.argv[1]
if len(nonce)!=32 or any(c not in "0123456789abcdef" for c in nonce): raise SystemExit(2)
directory="/tmp/fleetops-sudo-auth-"+nonce
fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    info=os.fstat(fd)
    fifo_info=os.stat("candidate",dir_fd=fd,follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700
        or not stat.S_ISFIFO(fifo_info.st_mode) or fifo_info.st_uid!=os.getuid() or stat.S_IMODE(fifo_info.st_mode)!=0o600): raise SystemExit(3)
    data=bytearray(sys.stdin.buffer.read(4099))
    try:
        if (len(data)<2 or len(data)>4098 or data[:1] not in (b"O",b"S")
            or not data.endswith(b"\n") or data.count(b"\n")!=1 or b"\0" in data or b"\r" in data): raise SystemExit(4)
        out=os.open("candidate",os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            if not stat.S_ISFIFO(os.fstat(out).st_mode): raise SystemExit(5)
            view=memoryview(data)
            while view:
                count=os.write(out,view)
                if count<=0: raise SystemExit(6)
                view=view[count:]
        finally: os.close(out)
    finally:
        for index in range(len(data)): data[index]=0
finally: os.close(fd)
'''

FIFO_WRITER_BOOTSTRAP = "exec(" + repr(FIFO_WRITER) + ")"


def deliver_candidate(transport: Any, target: str, nonce: str,
                      credential: str | None, *, environment: Mapping[str, str],
                      deadline: float, validate_current: Callable[[], bool]) -> bool:
    """Deliver one requested candidate or a skip marker through remote stdin."""
    if not callable(validate_current) or not math.isfinite(deadline) or time.monotonic() >= deadline:
        return False
    if credential is None:
        payload = bytearray(b"S\n")
    else:
        payload = bytearray(b"O" + credential.encode("utf-8", "strict") + b"\n")
    try:
        command = transport.command(target, ["python3", "-c", FIFO_WRITER_BOOTSTRAP, nonce])
        # Qualification and credential resolution may block. Revoke immediately
        # before starting the only process that receives the candidate bytes.
        if validate_current() is not True or time.monotonic() >= deadline:
            return False
        result = subprocess.run(
            command,
            input=bytes(payload), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=min(20.0, deadline-time.monotonic()),
            env=child_environment(environment),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        for index in range(len(payload)):
            payload[index] = 0

"""Strict SSH staging and readback for an exact one-off diagnostic claim."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from collections.abc import Sequence
from typing import Any

from ..herdr_ops.remote import _qualify_ssh_target, ssh_command
from .credentials import child_environment

IDENTITY = r'''import hashlib,json,os,pathlib
boot=pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
state=os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
print(json.dumps({"generation":hashlib.sha256((boot+":"+str(os.getuid())).encode()).hexdigest(),"state_home":state,"uid":os.getuid()}))'''

STAGE = r'''import hashlib,json,os,pathlib,stat,sys
payload=json.loads(sys.stdin.buffer.read(1048577)); root=pathlib.PurePosixPath(sys.argv[1])
boot=pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
if hashlib.sha256((boot+":"+str(os.getuid())).encode()).hexdigest()!=payload["claim"]["remote"]["process_generation"]: raise ValueError("remote machine generation changed")
if not root.is_absolute() or ".." in root.parts or str(root)=="/": raise ValueError("unsafe staging path")
fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY)
try:
    for part in root.parts[1:-1]:
        try: os.mkdir(part,0o700,dir_fd=fd)
        except FileExistsError: pass
        child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        info=os.fstat(child)
        if info.st_uid not in (0,os.getuid()) or (info.st_mode&0o022 and not (info.st_uid==0 and info.st_mode&stat.S_ISVTX)): raise ValueError("unsafe staging parent")
        os.close(fd); fd=child
    os.mkdir(root.name,0o700,dir_fd=fd)
    child=os.open(root.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        out=os.open("claim.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=child)
        raw=json.dumps(payload["claim"],sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
        with os.fdopen(out,"wb") as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.fsync(child)
    finally: os.close(child)
finally: os.close(fd)
print(json.dumps({"claim_sha256":hashlib.sha256(raw).hexdigest()}))'''


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _settings(target: str) -> dict[str, str]:
    effective = _qualify_ssh_target(target)
    executable = ssh_command(target, ["true"], timeout=20)[0]
    result = subprocess.run([executable, "-G", target], capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=20, check=False,
                            env=child_environment(os.environ))
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise ValueError("diagnostic SSH configuration is unavailable")
    digest = hashlib.sha256(result.stdout)
    for line in result.stdout.decode("utf-8").splitlines():
        key, _, value = line.partition(" ")
        if key not in {"userknownhostsfile", "globalknownhostsfile"}:
            continue
        for raw in shlex.split(value):
            if raw == "none":
                continue
            path = Path(raw.replace("%d", str(Path.home())).replace("%h", effective["hostname"])
                        .replace("%p", effective.get("port", "22"))
                        .replace("%r", effective.get("user", "")).replace("%u", os.environ.get("USER", ""))).expanduser()
            if "%" in str(path):
                raise ValueError("unsupported SSH known-hosts path token")
            digest.update(str(path).encode())
            try:
                digest.update(path.read_bytes())
            except FileNotFoundError:
                digest.update(b"absent")
    return effective | {"trust_sha256": digest.hexdigest()}


class DiagnosticSSH:
    """Requalify the frozen effective SSH target before every private delivery."""

    def __init__(self, target: str):
        self.target = target
        self.settings = _settings(target)

    def command(self, target: str, argv: Sequence[str]) -> list[str]:
        if target != self.target or _settings(target) != self.settings:
            raise ValueError("diagnostic SSH target configuration changed")
        return ssh_command(target, argv, timeout=20)

    def read(self, argv: Sequence[str], *, stdin: bytes | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
        result = subprocess.run(self.command(self.target, argv), capture_output=True,
                                timeout=20, check=False, env=child_environment(os.environ), **options)
        if result.returncode != 0:
            # Do not disclose remote stderr or exception messages.
            raise ValueError("diagnostic SSH observation failed; reconcile without replay")
        if len(result.stdout) > 1024 * 1024:
            raise ValueError("diagnostic SSH observation exceeds its bound")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("diagnostic SSH observation is not an object")
        return value

    def endpoint_digest(self) -> str:
        return hashlib.sha256(canonical(self.settings)).hexdigest()

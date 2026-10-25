"""Read-only snapshots from the existing local Codex daemon, scoped by ancestry."""
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import time

from ..application_profiles.service import _bounded_json
from ..framework_version import framework_version
from .command_state import process_info
from .command_activity import observation, TERMINAL

THREAD = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
METHODS = frozenset({"initialize", "thread/read", "thread/list", "thread/turns/list", "thread/items/list", "thread/items/read"})
SOURCES = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


class CodexObserver:
    def __init__(self, thread, owner, cwd):
        if not THREAD.fullmatch(thread):
            raise ValueError("invalid client session identity")
        self.root, self.owner, self.cwd = thread, owner, Path(cwd).resolve()
        self.connection = None
        self.sequence = 0
        self.threads = {thread}
        self.cursors = {}
        self.pending = {}

    def connect(self):
        from websockets.sync.client import unix_connect
        process = process_info(self.owner["pid"])
        if process is None or process["starttime"] != self.owner["starttime"]:
            raise ValueError("client owner is no longer live")
        executable = Path(os.readlink(f"/proc/{self.owner['pid']}/exe"))
        if executable.name != "codex":
            raise ValueError("native owner is not a Codex daemon")
        result = subprocess.run([str(executable), "app-server", "daemon", "version"],
            capture_output=True, timeout=5, check=True)
        native = _bounded_json(result.stdout, label="native daemon status", limit=65536)
        if native.get("status") != "running":
            raise ValueError("native daemon is unavailable")
        path = Path(native["socketPath"])
        if not path.is_absolute() or path.lstat().st_uid != os.getuid():
            raise ValueError("native socket identity is unavailable")
        # Codex owns a stable symlink to its per-generation private socket.
        # Qualify the target and then the live peer, rather than rejecting its
        # native discovery contract or trusting the pathname alone.
        path = path.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("native socket identity is unavailable")
        logger = logging.Logger("localsetup.client-observer", level=logging.CRITICAL)
        logger.addHandler(logging.NullHandler())
        connection = unix_connect(path, legacy=True, compression=None, max_size=4*1024*1024,
            max_queue=4, open_timeout=5, close_timeout=1, logger=logger)
        try:
            pid, uid, _gid = struct.unpack("3i", connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != self.owner["pid"] or uid != os.getuid():
                raise ValueError("native socket peer differs from the bound owner")
            self.connection = connection
            self.request("initialize", {"clientInfo": {"name": "localsetup_observer", "version": framework_version()},
                                       "capabilities": {"experimentalApi": True}})
            connection.send('{"method":"initialized","params":{}}')
            root = self.request("thread/read", {"threadId": self.root, "includeTurns": False})["thread"]
            if root["id"] != self.root or Path(root["cwd"]).resolve() != self.cwd:
                raise ValueError("native client workspace differs from this session")
        except BaseException:
            connection.close()
            self.connection = None
            raise

    def request(self, method, params):
        if method not in METHODS:
            raise PermissionError("observer RPC is not read-only")
        if method == "thread/list" and (params.get("ancestorThreadId") != self.root
                or params.get("useStateDbOnly") is not True
                or params.get("sourceKinds") != SOURCES):
            raise PermissionError("descendant query differs from the bound client family")
        if method.startswith("thread/") and method != "thread/list" and params.get("threadId") not in self.threads:
            raise PermissionError("thread is outside the bound client family")
        self.sequence += 1
        identity = self.sequence
        self.connection.send(json.dumps({"id": identity, "method": method, "params": params}))
        deadline = time.monotonic() + 5
        for _ in range(128):
            raw = self.connection.recv(timeout=max(.01, deadline-time.monotonic()))
            value = _bounded_json(raw.encode() if isinstance(raw, str) else raw,
                                  label="client observation", limit=4*1024*1024)
            if value.get("id") != identity:
                # Never answer approval/tool requests or retain unsolicited data.
                continue
            if "error" in value:
                raise RuntimeError("native observation method unavailable")
            return value["result"]
        raise RuntimeError("native observation response budget exceeded")

    def snapshot(self):
        # A later transport failure must not discard an earlier completion
        # removed from pending before the snapshot reaches its consumer.
        previous = self.threads, self.cursors, self.pending
        self.cursors, self.pending = dict(self.cursors), dict(self.pending)
        try:
            return self._snapshot()
        except BaseException:
            self.threads, self.cursors, self.pending = previous
            raise

    def _snapshot(self):
        result = self.request("thread/list", {"ancestorThreadId": self.root, "useStateDbOnly": True,
                                              "sourceKinds": SOURCES, "limit": 32})
        children = result["data"]
        identities = {item["id"] for item in children if isinstance(item.get("id"), str) and THREAD.fullmatch(item["id"])}
        self.threads = {self.root, *identities}
        labels = {self.root: "root"}
        for child in children:
            if child.get("id") in identities:
                label = child.get("agentNickname") or child.get("agentRole") or child["id"][:8]
                label = re.sub(r"[^A-Za-z0-9_-]", "", str(label))[:16] or child["id"][:8]
                labels[child["id"]] = "sub " + label
        observations, gap = [], bool(result.get("nextCursor"))
        for thread in sorted(self.threads):
            try:
                cursor = self.cursors.get(thread)
                for _page in range(4):
                    params = {"threadId": thread, "limit": 32,
                              "sortDirection": "asc" if cursor else "desc"}
                    if cursor:
                        params["cursor"] = cursor
                    items = self.request("thread/items/list", params)
                    entries = items["data"] if cursor else list(reversed(items["data"]))
                    observations.extend((thread, item) for item in entries)
                    for entry in entries:
                        item = entry["item"]
                        key = thread, entry["turnId"], item["id"]
                        value = observation(thread, entry)
                        if value is not None and value["state"] not in TERMINAL:
                            if len(self.pending) < 512:
                                self.pending[key] = None
                            else:
                                gap = True
                        else:
                            self.pending.pop(key, None)
                    if not cursor:
                        cursor = items.get("backwardsCursor")
                        gap |= bool(items.get("nextCursor"))  # Bounded initial history only.
                        self.cursors[thread] = cursor
                        break
                    if not items.get("nextCursor"):
                        break  # Retain this partial-tail cursor to catch later appends.
                    cursor = items["nextCursor"]
                    self.cursors[thread] = cursor
                else:
                    gap = True  # More queued metadata continues on the next refresh.
            except RuntimeError:
                if thread == self.root:
                    raise
                gap = True
                continue
        for thread, turn, identity in list(self.pending):
            if thread not in self.threads:
                self.pending.pop((thread, turn, identity), None)
                continue
            try:
                values = self.request("thread/items/read", {"threadId": thread, "turnId": turn, "itemIds": [identity]})["data"]
                for entry in values:
                    observations.append((thread, entry))
                    value = observation(thread, entry)
                    if value is None or value["state"] in TERMINAL:
                        self.pending.pop((thread, turn, identity), None)
            except RuntimeError:
                gap = True
        return labels, observations, gap

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

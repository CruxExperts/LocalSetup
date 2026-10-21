"""Strict, bounded LSCli event receipt; never a source of task authority."""
import json
import re

FRAME_LIMIT = 4 * 1024 * 1024
IDENTIFIER = re.compile(r"[a-zA-Z0-9_.:-]{1,128}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate protocol key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("nonfinite protocol number")


class Receipt:
    def __init__(self):
        self.buffer = bytearray()
        self.sequence = 0
        self.identity = None
        self.status = None
        self.checkpoint = None

    def feed(self, raw: bytes) -> bool:
        """Return whether a complete valid start/progress frame was observed."""
        active = False
        for part in raw.splitlines(keepends=True):
            if self.status is not None:
                raise ValueError("bytes after terminal result")
            self.buffer.extend(part)
            if len(self.buffer) > FRAME_LIMIT:
                raise ValueError("protocol frame limit")
            if not self.buffer.endswith(b"\n"):
                continue
            try:
                value = json.loads(self.buffer.decode("utf-8"), object_pairs_hook=_object,
                                   parse_constant=_constant)
            except (ValueError, RecursionError) as exc:
                raise ValueError("invalid protocol JSON") from exc
            self.buffer.clear()
            active = self._frame(value) or active
        return active

    def _frame(self, value) -> bool:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "sequence", "type", "data"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["sequence"]) is not int or value["sequence"] != self.sequence + 1
                or not isinstance(value["data"], dict)):
            raise ValueError("invalid protocol envelope")
        self.sequence += 1
        kind, data = value["type"], value["data"]
        if kind == "start":
            if (self.identity is not None or set(data) != {"task", "session", "profile"}
                    or any(not isinstance(data[k], str) or not IDENTIFIER.fullmatch(data[k])
                           for k in ("task", "session"))
                    or not isinstance(data["profile"], str) or not 1 <= len(data["profile"]) <= 256):
                raise ValueError("invalid protocol start")
            self.identity = {k: data[k] for k in ("task", "session", "profile")}
            return True
        if kind == "progress" and self.identity is not None:
            return True
        if kind != "result":
            raise ValueError("unexpected protocol event")
        status = data.get("status")
        if (not isinstance(status, str) or not IDENTIFIER.fullmatch(status)
                or not set(data) <= {"status", "task", "session", "output", "checkpoint"}):
            raise ValueError("invalid protocol result")
        if self.identity is not None:
            if any(data.get(k) != self.identity[k] for k in ("task", "session")):
                raise ValueError("protocol identity mismatch")
        elif set(data) != {"status"} or status == "completed":
            raise ValueError("completion requires start")
        if status == "completed":
            if (set(data) != {"status", "task", "session", "output", "checkpoint"}
                    or not isinstance(data["output"], str)
                    or not isinstance(data["checkpoint"], str)
                    or not DIGEST.fullmatch(data["checkpoint"])):
                raise ValueError("invalid completed result")
            self.checkpoint = data["checkpoint"]
        self.status = status
        return False

    def finish(self, returncode) -> dict:
        if self.buffer or self.status is None:
            raise ValueError("missing or truncated terminal result")
        return {"completed": returncode == 0 and self.status == "completed",
                "status": self.status, "sequence": self.sequence,
                "identity": self.identity, "checkpoint": self.checkpoint}

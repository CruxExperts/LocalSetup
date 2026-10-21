"""Stream multipart ranges and persist recoverable, non-replaying upload state."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .reporting import ToolError

CHUNK = 1024 * 1024


def _stat(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _identity(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ToolError("source_missing", "source must be a regular file")
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if _stat(before) != _stat(after):
        raise ToolError("source_changed", "source changed while its identity was measured")
    return {**_stat(after), "sha256": digest.hexdigest()}


def _safe_path(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ToolError("checkpoint_unsafe", "checkpoint path must not contain symlinks")


def _write(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    _safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".multipart-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ToolError("checkpoint_unavailable", "checkpoint could not be durably written") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path: Path) -> dict[str, Any]:
    _safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ToolError("checkpoint_unsafe", "checkpoint must be an owned regular mode 0600 file")
            raw = source.read(8 * CHUNK + 1)
        if len(raw) > 8 * CHUNK:
            raise ValueError("size")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("shape")
        return value
    except (OSError, ValueError) as exc:
        raise ToolError("checkpoint_invalid", "checkpoint cannot be read") from exc


class FileRange(io.RawIOBase):
    """Seekable request body; HTTP reads cannot allocate an entire large part."""
    def __init__(self, source: Any, offset: int, length: int):
        self.source, self.offset, self.length, self.position = source, offset, length, 0
        source.seek(offset)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def __len__(self) -> int:
        return self.length

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        position = offset if whence == 0 else (self.position if whence == 1 else self.length) + offset
        if whence not in (0, 1, 2) or not 0 <= position <= self.length:
            raise ValueError("part seek out of bounds")
        self.source.seek(self.offset + position)
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        size = min(CHUNK, self.length - self.position, size if size >= 0 else CHUNK)
        value = self.source.read(size)
        if not value and self.position < self.length:
            raise ToolError("source_changed", "source ended before the declared part length", "uncertain")
        self.position += len(value)
        return value


def _remote_parts(client: Any, args: dict[str, Any], state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    parts: dict[int, dict[str, Any]] = {}
    marker, seen = None, set()
    for _ in range(10001):
        from .encryption import parameters
        request = {"Bucket": args["bucket"], "Key": args["key"], "UploadId": state["upload_id"], **parameters(args.get("encryption"), customer_only=True)}
        if marker is not None:
            request["PartNumberMarker"] = marker
        response = client.list_parts(**request)
        if not isinstance(response, dict) or not isinstance(response.get("Parts", []), list):
            raise ToolError("malformed_response", "multipart part listing is malformed", "service")
        for item in response.get("Parts", []):
            if not isinstance(item, dict) or type(item.get("PartNumber")) is not int or not isinstance(item.get("ETag"), str):
                raise ToolError("malformed_response", "multipart part identity is malformed", "service")
            number = item["PartNumber"]
            if number in parts or not 1 <= number <= 10000:
                raise ToolError("malformed_response", "multipart part numbers are invalid", "service")
            parts[number] = item
        if not response.get("IsTruncated", False):
            return parts
        marker = response.get("NextPartNumberMarker")
        if type(marker) is not int or marker in seen:
            raise ToolError("repeated_continuation", "multipart part marker is missing or repeated", "service")
        seen.add(marker)
    raise ToolError("pagination_limit", "multipart part listing exceeded its bound", "service")


def _uncertain(message: str) -> ToolError:
    return ToolError("write_outcome_unknown", message, "uncertain", False,
                     "inspect ListParts, ListMultipartUploads and HeadObject; do not repeat completion or restart automatically")


def _upload(client: Any, args: dict[str, Any], account_marker: str, *, resume: bool = False) -> dict[str, Any]:
    from .encryption import parameters, fingerprint
    source, checkpoint = Path(args["source"]), Path(args["checkpoint"])
    identity = _identity(source)
    if identity["size"] == 0:
        raise ToolError("invalid_source", "use PutObject for an empty file")
    binding = {"provider": "garage", "account": account_marker,
               "bucket": args["bucket"], "key": args["key"], "source": identity}
    if resume:
        state = _load(checkpoint)
        if any(state.get(key) != value for key, value in binding.items()):
            raise ToolError("checkpoint_mismatch", "checkpoint does not match source, destination, or credential identity")
        if state.get("phase") in {"initializing", "completing", "completed", "source_changed"}:
            raise _uncertain("checkpoint records an initialization/completion or source state requiring read-only reconciliation")
        if state.get("schema_version") != 1 or state.get("phase") != "uploading" or not isinstance(state.get("upload_id"), str) or not state["upload_id"]:
            raise ToolError("checkpoint_invalid", "checkpoint phase or upload identity is invalid")
        part_size = state.get("part_size")
        if type(part_size) is not int or not 5 * 1024**2 <= part_size <= 5 * 1024**3:
            raise ToolError("checkpoint_invalid", "checkpoint part size is invalid")
        encryption = args.get("encryption")
        if fingerprint(encryption) != state.get("encryption_fingerprint"):
            raise ToolError("checkpoint_mismatch", "resume requires the same encryption reference and key")
        stored = state.get("parts")
        if not isinstance(stored, list) or any(not isinstance(p, dict) or set(p) != {"part_number", "etag"} or type(p["part_number"]) is not int or not isinstance(p["etag"], str) for p in stored):
            raise ToolError("checkpoint_invalid", "checkpoint part records are malformed")
        remote = _remote_parts(client, args, state)
        existing = {p["part_number"]: p["etag"] for p in stored}
        if len(existing) != len(stored) or set(remote) != set(existing) or any(remote[n]["ETag"] != tag for n, tag in existing.items()):
            raise _uncertain("remote parts differ from confirmed checkpoint parts; reconcile the uncertain part explicitly")
    else:
        part_size = args.get("part_size", 5 * 1024**2)
        encryption = args.get("encryption")
        state = {**binding, "schema_version": 1, "phase": "initializing", "part_size": part_size,
                 "encryption_fingerprint": fingerprint(encryption), "parts": []}
        existing = {}
    count = math.ceil(identity["size"] / part_size)
    if count > 10000:
        raise ToolError("too_many_parts", "source and part size exceed 10,000 parts")
    if not resume:
        _write(checkpoint, state, exclusive=True)
        request = {"Bucket": args["bucket"], "Key": args["key"], "ContentType": args.get("content_type", "application/octet-stream"),
                   "Metadata": args.get("metadata", {}), **parameters(encryption)}
        response = client.create_multipart_upload(**request)
        if not isinstance(response, dict) or not isinstance(response.get("UploadId"), str):
            raise _uncertain("multipart initiation response omitted its upload identity")
        state.update(upload_id=response["UploadId"], phase="uploading")
        try:
            _write(checkpoint, state)
        except ToolError:
            return {"partial": True, "upload_id": state["upload_id"], "reconciliation": "checkpoint delivery failed; inspect ListParts using this upload identity"}
    with source.open("rb") as handle:
        if _stat(os.fstat(handle.fileno())) != {k: identity[k] for k in _stat(os.fstat(handle.fileno()))}:
            raise ToolError("source_changed", "source changed before upload")
        for number in range(1, count + 1):
            if number in existing:
                continue
            offset = (number - 1) * part_size
            length = min(part_size, identity["size"] - offset)
            body = FileRange(handle, offset, length)
            response = client.upload_part(Bucket=args["bucket"], Key=args["key"], UploadId=state["upload_id"],
                PartNumber=number, Body=body, ContentLength=length, **parameters(encryption, customer_only=True))
            if not isinstance(response, dict) or not isinstance(response.get("ETag"), str):
                raise _uncertain("multipart part response omitted its identity")
            if _stat(os.fstat(handle.fileno())) != {k: identity[k] for k in _stat(os.fstat(handle.fileno()))}:
                state["phase"] = "source_changed"
                _write(checkpoint, state)
                raise _uncertain("source changed during part upload")
            existing[number] = response["ETag"]
            state["parts"] = [{"part_number": n, "etag": tag} for n, tag in sorted(existing.items())]
            try:
                _write(checkpoint, state)
            except ToolError:
                raise _uncertain("part may be stored but checkpoint persistence failed")
    state["phase"] = "completing"
    _write(checkpoint, state)
    response = client.complete_multipart_upload(Bucket=args["bucket"], Key=args["key"], UploadId=state["upload_id"],
        MultipartUpload={"Parts": [{"PartNumber": n, "ETag": tag} for n, tag in sorted(existing.items())]},
        **parameters(encryption, customer_only=True))
    if not isinstance(response, dict) or not isinstance(response.get("ETag"), str):
        raise _uncertain("multipart completion was not confirmed")
    state.update(phase="completed", result={"version_id": None, "etag": response["ETag"]})
    result = {"upload_id": state["upload_id"], **state["result"], "checkpoint": str(checkpoint),
              "content_length": identity["size"], "assurance": "source identity and confirmed parts; multipart ETag is not a whole-file hash"}
    try:
        _write(checkpoint, state)
    except ToolError:
        result.update(partial=True, reconciliation="completion succeeded but checkpoint update failed; inspect HeadObject for the destination")
    return result


def upload(client: Any, args: dict[str, Any], account_marker: str, *, resume: bool = False) -> dict[str, Any]:
    """An exclusive sidecar coordinates all mutations using this checkpoint."""
    lock = Path(args["checkpoint"] + ".lock")
    _safe_path(lock)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ToolError("checkpoint_busy", "checkpoint is in use; after a crash reconcile remote state before removing its lock", "policy") from exc
    except OSError as exc:
        raise ToolError("checkpoint_unavailable", "checkpoint lock cannot be reserved") from exc
    os.close(fd)
    try:
        return _upload(client, args, account_marker, resume=resume)
    finally:
        lock.unlink()

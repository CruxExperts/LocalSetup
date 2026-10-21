"""Stream, verify and publish downloads with exclusive local recovery files."""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from .encryption import parameters
from .reporting import ToolError


def download(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    destination = Path(args["destination"])
    for parent in (destination, *destination.parents):
        if parent.is_symlink():
            raise ToolError("destination_unsafe", "download path must not contain symlinks", "policy")
    if destination.exists() and not args.get("overwrite"):
        raise ToolError("destination_exists", "download destination exists", "policy")
    request = {"Bucket": args["bucket"], "Key": args["key"], **parameters(args.get("encryption"), customer_only=True)}
    for field, wire in (("version_id", "VersionId"), ("range", "Range")):
        if field in args:
            request[wire] = args[field]
    response = client.get_object(**request)
    if not isinstance(response, dict) or type(response.get("ContentLength")) is not int or response["ContentLength"] < 0 or not callable(getattr(response.get("Body"), "read", None)):
        raise ToolError("malformed_response", "download response omitted a stream or valid content length", "service")
    stream = response["Body"]
    temporary: str | None = None
    backup: Path | None = None
    size, digest = 0, hashlib.sha256()
    try:
        fd, temporary = tempfile.mkstemp(prefix=".garage-", dir=destination.parent)
        with os.fdopen(fd, "wb") as output:
            while True:
                deadline = getattr(client, "deadline", None)
                if isinstance(deadline, (int, float)) and time.monotonic() >= deadline:
                    raise ToolError("request_budget_expired", "download budget expired", "service", True)
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ToolError("malformed_response", "download stream returned invalid bytes", "service")
                output.write(chunk)
                size += len(chunk)
                digest.update(chunk)
                if size > response["ContentLength"]:
                    raise ToolError("integrity_failed", "download exceeded declared content length", "service")
            output.flush()
            os.fsync(output.fileno())
        if size != response["ContentLength"]:
            raise ToolError("integrity_failed", "download length did not match server response", "service")
        checksum = response.get("ChecksumSHA256")
        verified = False
        if checksum and response.get("ChecksumType") != "COMPOSITE" and "-" not in checksum:
            if base64.b64encode(digest.digest()).decode() != checksum:
                raise ToolError("integrity_failed", "download SHA256 did not match provider checksum", "service")
            verified = True
        # No-clobber remains exclusive even if another writer appeared after GET.
        if not args.get("overwrite"):
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ToolError("destination_exists", "download destination appeared during transfer", "policy") from exc
            os.unlink(temporary)
        elif destination.exists() or destination.is_symlink():
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("destination_unsafe", "overwrite destination must be a regular file", "policy")
            backup = destination.with_name(destination.name + ".garage-backup")
            try:
                os.link(destination, backup, follow_symlinks=False)
            except FileExistsError as exc:
                raise ToolError("backup_exists", "exclusive download backup already exists", "policy") from exc
            if backup.lstat().st_ino != info.st_ino or destination.lstat().st_ino != info.st_ino:
                raise ToolError("destination_changed", "destination changed while preserving backup", "policy")
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ToolError("destination_changed", "destination appeared during publication; retry with explicit intent", "policy") from exc
            os.unlink(temporary)
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise ToolError("local_publish_unknown", "download was published but directory durability was not confirmed", "uncertain", False, "inspect destination and its exclusive backup before retrying") from exc
        finally:
            os.close(directory_fd)
        return {"destination": str(destination), "backup": str(backup) if backup else None, "content_length": size, "version_id": None, "checksum": checksum, "checksum_verified": verified, "sha256": digest.hexdigest(), "assurance": "verified length and provider SHA256" if verified else "verified length; locally computed SHA256; provider checksum unverified or absent"}
    except ToolError:
        raise
    except (OSError, ValueError) as exc:
        raise ToolError("download_failed", "download failed during transfer or local publication", "service", False) from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass

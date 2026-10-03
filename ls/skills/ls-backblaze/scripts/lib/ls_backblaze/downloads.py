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


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checked_lstat(path: Path, message: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ToolError("destination_changed", message, "policy") from exc


def _same_content_state(left: os.stat_result, right: os.stat_result) -> bool:
    # Rename can legitimately update ctime, so displacement checks compare content metadata.
    return (left.st_dev, left.st_ino, left.st_mode, left.st_size, left.st_mtime_ns) == (
        right.st_dev, right.st_ino, right.st_mode, right.st_size, right.st_mtime_ns
    )


def _sync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _backup_path(destination: Path) -> Path:
    suffix_name = destination.name + ".backblaze-backup"
    try:
        component_limit = os.pathconf(destination.parent, "PC_NAME_MAX")
    except (OSError, ValueError):
        component_limit = 255
    if component_limit <= 0:
        component_limit = 255
    if len(os.fsencode(suffix_name)) <= component_limit:
        return destination.with_name(suffix_name)
    digest = hashlib.sha256(os.fsencode(destination.name)).hexdigest()[:16]
    return destination.with_name(f".backblaze-backup-{digest}")


def _reconciliation(
    destination: Path,
    backup: Path | None = None,
    recovery_original: Path | None = None,
    temporary: Path | None = None,
) -> str:
    paths = [f"destination={destination}"]
    if backup is not None:
        paths.append(f"independent backup={backup}")
    if recovery_original is not None:
        paths.extend((f"private recovery directory={recovery_original.parent}", f"private recovery original={recovery_original}"))
    if temporary is not None:
        paths.append(f"temporary download={temporary}")
    return "Inspect these exact paths before retrying: " + "; ".join(paths)


def _attach_reconciliation(
    error: ToolError,
    destination: Path,
    backup: Path | None = None,
    recovery_original: Path | None = None,
    temporary: Path | None = None,
) -> None:
    exact_paths = _reconciliation(destination, backup, recovery_original, temporary)
    if error.reconciliation:
        error.reconciliation = f"{error.reconciliation}; {exact_paths}"
    else:
        error.reconciliation = exact_paths


def _restore_displaced(recovery_original: Path, destination: Path, backup: Path) -> bool:
    """Restore only into a vacant name; recovery_original is deliberately retained."""
    try:
        os.link(recovery_original, destination, follow_symlinks=False)
    except FileExistsError:
        return False
    except OSError as exc:
        raise ToolError(
            "local_publish_unknown",
            "the original remains in private recovery, but restoring the destination failed",
            "uncertain",
            False,
            _reconciliation(destination, backup, recovery_original),
        ) from exc
    try:
        _sync_directory(destination.parent)
    except OSError as exc:
        raise ToolError(
            "local_publish_unknown",
            "the original was restored, but directory durability was not confirmed",
            "uncertain",
            False,
            _reconciliation(destination, backup, recovery_original),
        ) from exc
    return True


def _preserve_existing(destination: Path, expected: tuple[int, int, int, int, int, int]) -> tuple[Path, Path, Path, tuple[int, int, int, int, int, int]]:
    """Make a stable independent backup, then displace the original without clobbering recovery."""
    backup = _backup_path(destination)
    path_info = _checked_lstat(destination, "overwrite destination disappeared before preservation")
    if not stat.S_ISREG(path_info.st_mode):
        raise ToolError("destination_unsafe", "overwrite destination must be a regular file", "policy")
    if _fingerprint(path_info) != expected:
        raise ToolError("destination_changed", "overwrite destination changed during transfer", "policy")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(destination, flags)
    except OSError as exc:
        raise ToolError("destination_changed", "overwrite destination changed before preservation", "policy") from exc

    backup_temporary: str | None = None
    recovery_directory: Path | None = None
    recovery_original: Path | None = None
    moved = False
    try:
        initial = os.fstat(source_fd)
        if not stat.S_ISREG(initial.st_mode) or _fingerprint(initial) != expected:
            raise ToolError("destination_changed", "overwrite destination changed before preservation", "policy")
        if backup.exists() or backup.is_symlink():
            raise ToolError("backup_exists", "exclusive download backup already exists", "policy")

        backup_fd, backup_temporary = tempfile.mkstemp(prefix=".backblaze-backup-", dir=destination.parent)
        copied = 0
        with os.fdopen(backup_fd, "wb") as output:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                output.write(block)
                copied += len(block)
            output.flush()
            os.fsync(output.fileno())

        after_copy = os.fstat(source_fd)
        if _fingerprint(after_copy) != _fingerprint(initial) or copied != initial.st_size:
            raise ToolError("destination_changed", "overwrite destination changed while copying its backup", "policy")
        current = _checked_lstat(destination, "overwrite destination disappeared while copying its backup")
        if _fingerprint(current) != _fingerprint(initial):
            raise ToolError("destination_changed", "overwrite destination path changed while copying its backup", "policy")

        try:
            os.link(backup_temporary, backup, follow_symlinks=False)
        except FileExistsError as exc:
            raise ToolError("backup_exists", "exclusive download backup already exists", "policy") from exc
        _sync_directory(destination.parent)
        current = _checked_lstat(destination, "overwrite destination disappeared after its backup was copied")
        if _fingerprint(os.fstat(source_fd)) != _fingerprint(initial) or _fingerprint(current) != _fingerprint(initial):
            raise ToolError("destination_changed", "overwrite destination changed after its backup was copied", "policy")

        recovery_directory = Path(tempfile.mkdtemp(prefix=".backblaze-recovery-", dir=destination.parent))
        recovery_original = recovery_directory / "original"
        try:
            os.rename(destination, recovery_original)
            moved = True
        except OSError as exc:
            # A wrapper/filesystem can report an error after completing the rename.
            moved = recovery_original.exists() or recovery_original.is_symlink()
            if moved:
                _restore_displaced(recovery_original, destination, backup)
            else:
                try:
                    recovery_directory.rmdir()
                except OSError:
                    pass
            raise ToolError("destination_changed", "overwrite destination could not be safely displaced", "policy") from exc

        try:
            displaced_info = recovery_original.lstat()
        except OSError as exc:
            _restore_displaced(recovery_original, destination, backup)
            raise ToolError("local_publish_unknown", "the original was displaced but could not be inspected in private recovery", "uncertain", False) from exc
        if not stat.S_ISREG(displaced_info.st_mode) or not _same_content_state(displaced_info, initial):
            _restore_displaced(recovery_original, destination, backup)
            raise ToolError("destination_changed", "overwrite destination changed while being displaced; its entry was preserved in recovery", "policy")
        try:
            _sync_directory(recovery_directory)
            _sync_directory(destination.parent)
        except OSError as exc:
            _restore_displaced(recovery_original, destination, backup)
            raise ToolError("local_publish_unknown", "the original is preserved in private recovery, but its displacement was not confirmed durable", "uncertain", False) from exc
        return backup, recovery_directory, recovery_original, _fingerprint(displaced_info)
    except ToolError as exc:
        _attach_reconciliation(exc, destination, backup, recovery_original)
        raise
    except OSError as exc:
        raise ToolError(
            "download_failed",
            "download failed while preserving the overwrite destination",
            "service",
            False,
            _reconciliation(destination, backup, recovery_original),
        ) from exc
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass
        if backup_temporary is not None:
            try:
                os.unlink(backup_temporary)
            except OSError:
                pass
        if recovery_directory is not None and not moved:
            try:
                recovery_directory.rmdir()
            except OSError:
                pass


def download(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    destination = Path(args["destination"])
    for parent in (destination, *destination.parents):
        if parent.is_symlink():
            raise ToolError("destination_unsafe", "download path must not contain symlinks", "policy")
    destination_info: os.stat_result | None = None
    if destination.exists() or destination.is_symlink():
        if not args.get("overwrite"):
            raise ToolError("destination_exists", "download destination exists", "policy")
        destination_info = _checked_lstat(destination, "overwrite destination changed before transfer")
        if not stat.S_ISREG(destination_info.st_mode):
            raise ToolError("destination_unsafe", "overwrite destination must be a regular file", "policy")
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
    recovery: Path | None = None
    recovery_original: Path | None = None
    recovery_state: tuple[int, int, int, int, int, int] | None = None
    displaced = False
    published = False
    size, digest = 0, hashlib.sha256()
    try:
        fd, temporary = tempfile.mkstemp(prefix=".backblaze-", dir=destination.parent)
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

        if destination_info is not None:
            backup, recovery, recovery_original, recovery_state = _preserve_existing(destination, _fingerprint(destination_info))
            displaced = True
            if _fingerprint(recovery_original.lstat()) != recovery_state:
                _restore_displaced(recovery_original, destination, backup)
                raise ToolError("destination_changed", "displaced original changed before publication and was retained in recovery", "policy")
        try:
            os.link(temporary, destination, follow_symlinks=False)
            published = True
        except FileExistsError as exc:
            code = "destination_changed" if displaced or args.get("overwrite") else "destination_exists"
            message = "destination appeared during publication; its entry and any displaced original were preserved" if displaced or args.get("overwrite") else "download destination appeared during transfer"
            raise ToolError(code, message, "policy") from exc
        try:
            os.unlink(temporary)
            temporary = None
        except OSError as exc:
            raise ToolError("local_publish_unknown", "download was published but its temporary link could not be removed", "uncertain", False, "inspect the destination, temporary download file, backup, and recovery file before retrying") from exc
        try:
            _sync_directory(destination.parent)
        except OSError as exc:
            raise ToolError("local_publish_unknown", "download was published but directory durability was not confirmed", "uncertain", False, "inspect the destination and its exclusive backup and recovery file before retrying") from exc
        result = {
            "destination": str(destination),
            "backup": str(backup) if backup else None,
            "content_length": size,
            "version_id": response.get("VersionId"),
            "checksum": checksum,
            "checksum_verified": verified,
            "sha256": digest.hexdigest(),
            "assurance": "verified length and provider SHA256" if verified else "verified length; locally computed SHA256; provider checksum unverified or absent",
        }
        if args.get("include_recovery_path"):
            result["recovery"] = str(recovery) if recovery else None
        return result
    except ToolError as exc:
        if displaced and not published and recovery_original is not None:
            try:
                restored = _restore_displaced(recovery_original, destination, backup)
            except ToolError as restore_error:
                _attach_reconciliation(restore_error, destination, backup, recovery_original, Path(temporary) if temporary else None)
                raise restore_error from exc
            _attach_reconciliation(exc, destination, backup, recovery_original, Path(temporary) if temporary else None)
            if not restored:
                exc.message = f"{exc.message}; a competing destination remains in place and the displaced original is retained in private recovery"
                exc.args = (exc.message,)
        elif backup is not None or published:
            _attach_reconciliation(exc, destination, backup, recovery_original, Path(temporary) if temporary else None)
        raise
    except (OSError, ValueError) as exc:
        if displaced and not published and recovery_original is not None:
            try:
                restored = _restore_displaced(recovery_original, destination, backup)
            except ToolError as restore_error:
                _attach_reconciliation(restore_error, destination, backup, recovery_original, Path(temporary) if temporary else None)
                raise restore_error from exc
            if not restored:
                raise ToolError(
                    "local_publish_unknown",
                    "publication failed while another destination entry was present; the original remains in private recovery",
                    "uncertain",
                    False,
                    _reconciliation(destination, backup, recovery_original, Path(temporary) if temporary else None),
                ) from exc
        raise ToolError(
            "download_failed",
            "download failed during transfer or local publication",
            "service",
            False,
            _reconciliation(destination, backup, recovery_original, Path(temporary) if temporary else None) if backup is not None else None,
        ) from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass

from __future__ import annotations

import json
import os
from copy import copy
from pathlib import Path
import re
import stat
import uuid
from typing import Any, Callable

from ..paths import global_layout
from .adapter import GitHubError
from .maintenance_config import (
    CONFIG_RELATIVE_PATH,
    WORKFLOW_RELATIVE_PATH,
    MaintenanceError,
    canonical_config_bytes,
    config_digest,
    parse_config_bytes,
    read_config,
    render_shared_workflow,
)
from .maintenance_discovery import DiscoveryError, checkout_for_repository, discover_checkouts, discover_public_repositories
from .model import RepositoryTarget, digest_json
from .policy import PolicyError, parse_target
from .state import open_private_directory, operation_state_directory, target_operation_lock
from .maintenance_service import (
    MaintenanceOperationError,
    _adapter,
    _assert_config_binding,
    _path_for,
    _read_target_bytes,
    _repo_metadata,
    _sha256,
    _tracked,
    _target_origin_matches,
    _unexpected_tracked_maintenance_files,
)

_PLAN_TYPE = "localsetup.github-maintenance-setup"
_PLAN_SCHEMA = 1
_PLAN_LIMIT = 2 * 1024 * 1024


class _DiscoveryIdentityMismatch(MaintenanceOperationError):
    """The fleet discovery ID changed before an individual plan was prepared."""


def _plan_directory(home: Path, hostname: str, repository_id: int) -> Path:
    state_directory = operation_state_directory(global_layout(home).state_root, hostname, repository_id)
    directory = state_directory / "maintenance" / "plans"
    descriptor = open_private_directory(directory, create=True)
    os.close(descriptor)
    return directory


def _write_private_plan(directory: Path, plan: dict[str, Any]) -> Path:
    raw = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(raw) > _PLAN_LIMIT:
        raise MaintenanceOperationError("maintenance setup plan exceeds its private state limit")
    name = f"{plan['plan_digest']}.json"
    dir_fd = open_private_directory(directory, create=False)
    try:
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            existing = _read_private_file(directory, name)
            if existing != raw:
                raise MaintenanceOperationError("a different private setup plan already uses this digest")
            return directory / name
        os.fchmod(fd, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.fsync(dir_fd)
    except MaintenanceOperationError:
        raise
    except OSError as exc:
        raise MaintenanceOperationError("private setup plan could not be saved safely") from exc
    finally:
        os.close(dir_fd)
    return directory / name


def _read_private_file(directory: Path, name: str) -> bytes:
    dir_fd = open_private_directory(directory, create=False)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except OSError as exc:
            raise MaintenanceOperationError("authorized private setup plan is missing or unsafe") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _PLAN_LIMIT
            ):
                raise MaintenanceOperationError("authorized private setup plan is not a private regular file")
            chunks: list[bytes] = []
            remaining = _PLAN_LIMIT + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _load_private_plan(directory: Path, plan_digest: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", plan_digest):
        raise MaintenanceOperationError("--authorize-plan must be a full lowercase SHA-256 digest")
    raw = _read_private_file(directory, f"{plan_digest}.json")
    try:
        plan = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_plan_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MaintenanceOperationError("authorized private setup plan is invalid") from exc
    if (
        not isinstance(plan, dict)
        or type(plan.get("schema")) is not int
        or plan.get("schema") != _PLAN_SCHEMA
        or plan.get("plan_type") != _PLAN_TYPE
        or plan.get("plan_digest") != plan_digest
        or set(plan) != {"schema", "plan_type", "target", "config_digest", "files", "plan_digest"}
    ):
        raise MaintenanceOperationError("authorized private setup plan has an unsupported shape")
    unsigned = {key: item for key, item in plan.items() if key != "plan_digest"}
    if digest_json(unsigned) != plan_digest:
        raise MaintenanceOperationError("authorized private setup plan digest does not match its contents")
    return plan


def _unique_plan_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate plan key")
        value[key] = item
    return value


def _persisted_plan(
    target: RepositoryTarget,
    metadata: dict[str, Any],
    config: dict[str, Any],
    desired_files: dict[str, bytes],
    before_hashes: dict[str, str | None],
) -> dict[str, Any]:
    file_records = {
        relative: {
            "before_sha256": before_hashes[relative],
            "desired_sha256": _sha256(content),
            "content": content.decode("utf-8"),
        }
        for relative, content in sorted(desired_files.items())
    }
    plan = {
        "schema": _PLAN_SCHEMA,
        "plan_type": _PLAN_TYPE,
        "target": {
            "hostname": target.hostname,
            "repository_id": metadata["id"],
            "full_name": metadata["full_name"],
        },
        "config_digest": config_digest(config),
        "files": file_records,
    }
    plan["plan_digest"] = digest_json(plan)
    return plan


def _load_setup_config(args: Any, checkout: Path) -> tuple[dict[str, Any], bytes]:
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else _path_for(checkout, CONFIG_RELATIVE_PATH)
    try:
        return read_config(config_path)
    except MaintenanceError:
        raise


def plan_setup(
    args: Any,
    home: Path,
    *,
    adapter_factory: Callable[[RepositoryTarget], Any] = _adapter,
    expected_repository_id: int | None = None,
) -> dict[str, Any]:
    try:
        target = parse_target(args.hostname, args.repository)
    except PolicyError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    checkout = Path(args.checkout).expanduser().resolve(strict=False)
    _assert_all_public_selection(args, target, adapter_factory=adapter_factory)
    if not _target_origin_matches(checkout, target):
        raise MaintenanceOperationError("checkout origin must identify the selected GitHub repository")
    adapter = adapter_factory(target)
    metadata = _repo_metadata(adapter, target)
    if expected_repository_id is not None and metadata["id"] != expected_repository_id:
        raise _DiscoveryIdentityMismatch("public discovery and repository lookup returned different immutable IDs")
    config, _ = _load_setup_config(args, checkout)
    _assert_config_binding(config, target, metadata)
    unexpected = _unexpected_tracked_maintenance_files(checkout)
    if unexpected is None:
        raise MaintenanceOperationError("tracked maintenance files could not be checked safely")
    if unexpected:
        raise MaintenanceOperationError("unexpected tracked maintenance files conflict with the selected setup")
    target_config_raw = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
    if target_config_raw is not None:
        try:
            existing_config = parse_config_bytes(target_config_raw)
        except MaintenanceError as exc:
            raise MaintenanceOperationError("existing maintenance config is custom or invalid; refusing to overwrite it") from exc
        if existing_config != config:
            raise MaintenanceOperationError("existing maintenance config conflicts with the reviewed config")
        config_bytes = target_config_raw
    else:
        config_bytes = canonical_config_bytes(config)
    workflow_bytes = render_shared_workflow(config)
    desired_files = {
        CONFIG_RELATIVE_PATH: config_bytes,
        WORKFLOW_RELATIVE_PATH: workflow_bytes,
    }
    before_hashes: dict[str, str | None] = {}
    current_files: dict[str, bytes | None] = {}
    state_directory = operation_state_directory(global_layout(home).state_root, target.hostname, metadata["id"])
    with target_operation_lock(state_directory):
        for relative, desired in desired_files.items():
            current = _read_target_bytes(checkout, relative)
            current_files[relative] = current
            before_hashes[relative] = _sha256(current)
            if current is not None and current != desired:
                raise MaintenanceOperationError(f"existing {relative} conflicts with the generated maintenance file")
        plan = _persisted_plan(target, metadata, config, desired_files, before_hashes)
        plan_path = _write_private_plan(_plan_directory(home, target.hostname, metadata["id"]), plan)
    return {
        "status": "planned",
        "target": plan["target"],
        "config_digest": plan["config_digest"],
        "plan_digest": plan["plan_digest"],
        "before_hashes": before_hashes,
        "desired_hashes": {relative: _sha256(value) for relative, value in desired_files.items()},
        "plan_path": str(plan_path),
        "unchanged": all(current_files[relative] == desired_files[relative] for relative in desired_files),
    }


def _safe_config_directory(value: str | Path) -> Path:
    directory = Path(value).expanduser()
    try:
        info = directory.lstat()
    except OSError as exc:
        raise MaintenanceOperationError("config directory is missing or unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MaintenanceOperationError("config directory must be a real directory, not a symlink")
    try:
        return directory.resolve(strict=True)
    except OSError as exc:
        raise MaintenanceOperationError("config directory could not be resolved safely") from exc


def _fleet_config_path(
    repository_id: int,
    checkout: Path,
    config_directory: Path | None,
) -> tuple[Path | None, str | None]:
    if config_directory is not None:
        path = config_directory / f"{repository_id}.json"
        try:
            path.lstat()
        except FileNotFoundError:
            return None, "config_missing"
        except OSError:
            return None, "config_unreadable"
        try:
            read_config(path)
        except MaintenanceError:
            return None, "config_invalid_or_unsafe"
        return path, None
    if not _tracked(checkout, CONFIG_RELATIVE_PATH):
        return None, "tracked_config_missing"
    try:
        path = _path_for(checkout, CONFIG_RELATIVE_PATH)
        read_config(path)
    except (MaintenanceError, MaintenanceOperationError):
        return None, "config_invalid_or_unsafe"
    return None, None


def plan_all_public_setup(
    args: Any,
    home: Path,
    *,
    adapter_factory: Callable[[RepositoryTarget], Any] = _adapter,
) -> dict[str, Any]:
    owner = getattr(args, "owner", None)
    checkout_root = getattr(args, "checkout_root", None)
    if not owner or not checkout_root:
        raise MaintenanceOperationError("fleet setup requires --owner OWNER and --checkout-root ROOT")
    if getattr(args, "apply", False):
        raise MaintenanceOperationError(
            "fleet setup apply is not supported; apply reviewed child plans one repository at a time with --authorize-plan"
        )
    if getattr(args, "config", None):
        raise MaintenanceOperationError("fleet setup accepts per-repository <repository_id>.json files via --config-directory")
    config_directory_value = getattr(args, "config_directory", None)
    config_directory = _safe_config_directory(config_directory_value) if config_directory_value else None
    hostname = getattr(args, "hostname", "github.com")
    try:
        repositories = discover_public_repositories(hostname, owner, adapter_factory=adapter_factory)
        checkouts = discover_checkouts(Path(checkout_root), hostname)
    except DiscoveryError as exc:
        raise MaintenanceOperationError(str(exc)) from exc

    results: list[dict[str, Any]] = []
    for repository in repositories:
        try:
            parse_target(hostname, repository.full_name)
        except PolicyError:
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": "invalid_identity",
                "reason": "discovery_identity_invalid",
                "plan_digest": None,
            })
            continue
        checkout = checkout_for_repository(checkouts, repository.full_name)
        config_path: Path | None = None
        config_status = "unknown_no_checkout"
        config_error: str | None = None
        if checkout is not None:
            config_path, config_error = _fleet_config_path(repository.repository_id, checkout, config_directory)
            config_status = config_error or "available"
        elif config_directory is not None:
            config_path, config_error = _fleet_config_path(repository.repository_id, Path(), config_directory)
            config_status = config_error or "available"
        if checkout is None:
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": "missing_checkout",
                "reason": "checkout_missing_or_ambiguous",
                "checkout_status": "missing_or_ambiguous",
                "config_status": config_status,
                "plan_digest": None,
            })
            continue
        if config_error:
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": config_error,
                "reason": config_error,
                "checkout_status": "available",
                "config_status": config_status,
                "plan_digest": None,
            })
            continue
        child_args = copy(args)
        child_args.hostname = hostname
        child_args.repository = repository.full_name
        child_args.checkout = str(checkout)
        child_args.config = str(config_path) if config_path is not None else None
        child_args.all_public = False
        child_args.owner = None
        child_args.checkout_root = None
        child_args.config_directory = None
        child_args.apply = False
        child_args.authorize_plan = None
        try:
            child = plan_setup(
                child_args, home, adapter_factory=adapter_factory,
                expected_repository_id=repository.repository_id,
            )
        except _DiscoveryIdentityMismatch:
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": "identity_mismatch",
                "reason": "discovery_identity_mismatch",
                "checkout_status": "available",
                "config_status": config_status,
                "plan_digest": None,
            })
            continue
        except (MaintenanceOperationError, MaintenanceError, GitHubError):
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": "plan_rejected",
                "reason": "identity_or_file_conflict",
                "checkout_status": "available",
                "config_status": config_status,
                "plan_digest": None,
            })
            continue
        child_target = child.get("target")
        if not isinstance(child_target, dict) or child_target.get("repository_id") != repository.repository_id:
            results.append({
                "repository_id": repository.repository_id,
                "full_name": repository.full_name,
                "status": "identity_mismatch",
                "reason": "discovery_identity_mismatch",
                "checkout_status": "available",
                "config_status": config_status,
                "plan_digest": None,
            })
            continue
        results.append({
            "repository_id": repository.repository_id,
            "full_name": repository.full_name,
            "status": "planned",
            "checkout_status": "available",
            "config_status": config_status,
            "config_digest": child["config_digest"],
            "plan_digest": child["plan_digest"],
            "before_hashes": child["before_hashes"],
            "desired_hashes": child["desired_hashes"],
            "unchanged": child["unchanged"],
        })

    aggregate_digest = digest_json({
        "schema": 1,
        "hostname": hostname,
        "owner": owner.casefold(),
        "repositories": [
            {
                "repository_id": item["repository_id"],
                "full_name": item["full_name"],
                "plan_digest": item["plan_digest"],
            }
            for item in sorted(results, key=lambda row: row["repository_id"])
        ],
    })
    planned_count = sum(item["plan_digest"] is not None for item in results)
    incomplete_count = len(results) - planned_count
    return {
        "status": "planned" if results and incomplete_count == 0 else "incomplete",
        "scope": "all_public",
        "owner": owner,
        "repository_count": len(repositories),
        "discovered_repository_ids": [repository.repository_id for repository in repositories],
        "planned_count": planned_count,
        "incomplete_count": incomplete_count,
        "aggregate_digest": aggregate_digest,
        "fleet_apply_supported": False,
        "apply_guidance": "Apply each reviewed child plan one repository at a time with --repository, --checkout, --apply, and that row's --authorize-plan digest.",
        "repositories": results,
    }


def _assert_all_public_selection(
    args: Any,
    target: RepositoryTarget,
    *,
    adapter_factory: Callable[[RepositoryTarget], Any] = _adapter,
) -> None:
    if not bool(getattr(args, "all_public", False)):
        if getattr(args, "owner", None) or getattr(args, "checkout_root", None):
            raise MaintenanceOperationError("--owner and --checkout-root require --all-public")
        return
    owner = getattr(args, "owner", None)
    root = getattr(args, "checkout_root", None)
    if not owner or not root or owner.casefold() != target.owner.casefold():
        raise MaintenanceOperationError("setup --all-public requires the matching --owner and --checkout-root")
    try:
        repositories = discover_public_repositories(target.hostname, owner, adapter_factory=adapter_factory)
        checkout_map = discover_checkouts(Path(root), target.hostname)
    except DiscoveryError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    matches = [repo for repo in repositories if repo.full_name.casefold() == target.full_name.casefold()]
    checkout = Path(args.checkout).expanduser().resolve(strict=False)
    mapped = checkout_for_repository(checkout_map, target.full_name)
    if len(matches) != 1 or matches[0].repository_id <= 0:
        raise MaintenanceOperationError("selected repository is not present in the fully paginated public owner listing")
    if mapped is None or mapped.resolve(strict=False) != checkout:
        raise MaintenanceOperationError("selected checkout is missing or ambiguous in --checkout-root origin discovery")


def _write_target_file(checkout: Path, relative: str, content: bytes) -> None:
    path = _path_for(checkout, relative, create_parents=True)
    mode = 0o644
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MaintenanceOperationError("checkout target changed to a symlink or non-regular file")
        mode = stat.S_IMODE(info.st_mode)
    except FileNotFoundError:
        pass
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except MaintenanceOperationError:
        raise
    except OSError as exc:
        raise MaintenanceOperationError("maintenance file write failed; reconcile the checkout before retrying") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def apply_setup(
    args: Any,
    home: Path,
    *,
    adapter_factory: Callable[[RepositoryTarget], Any] = _adapter,
) -> dict[str, Any]:
    if not getattr(args, "authorize_plan", None):
        raise MaintenanceOperationError("--apply requires --authorize-plan DIGEST")
    try:
        target = parse_target(args.hostname, args.repository)
    except PolicyError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    checkout = Path(args.checkout).expanduser().resolve(strict=False)
    _assert_all_public_selection(args, target, adapter_factory=adapter_factory)
    if not _target_origin_matches(checkout, target):
        raise MaintenanceOperationError("checkout origin must identify the selected GitHub repository")
    adapter = adapter_factory(target)
    metadata = _repo_metadata(adapter, target)
    repository_id = metadata["id"]
    state_root = global_layout(home).state_root
    plan = _load_private_plan(_plan_directory(home, target.hostname, repository_id), args.authorize_plan)
    if plan.get("target", {}).get("hostname") != target.hostname or plan.get("target", {}).get("repository_id") != repository_id:
        raise MaintenanceOperationError("authorized setup plan is bound to another GitHub repository")
    state_directory = operation_state_directory(state_root, target.hostname, repository_id)
    if metadata["id"] != repository_id or metadata["full_name"] != plan["target"].get("full_name"):
        raise MaintenanceOperationError("GitHub repository identity changed after setup planning")
    file_records = plan.get("files")
    if not isinstance(file_records, dict) or set(file_records) != {CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH}:
        raise MaintenanceOperationError("authorized private setup plan does not contain the expected files")
    desired_files: dict[str, bytes] = {}
    for relative, record in file_records.items():
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            raise MaintenanceOperationError("authorized private setup plan contains an invalid file record")
        content = record["content"].encode("utf-8")
        if _sha256(content) != record.get("desired_sha256"):
            raise MaintenanceOperationError("authorized private setup plan file digest does not match its content")
        desired_files[relative] = content
    config = parse_config_bytes(desired_files[CONFIG_RELATIVE_PATH])
    _assert_config_binding(config, target, metadata)
    if config_digest(config) != plan.get("config_digest"):
        raise MaintenanceOperationError("authorized private setup plan config digest does not match")
    with target_operation_lock(state_directory):
        unexpected = _unexpected_tracked_maintenance_files(checkout)
        if unexpected is None:
            raise MaintenanceOperationError("tracked maintenance files could not be checked safely")
        if unexpected:
            raise MaintenanceOperationError("unexpected tracked maintenance files were added after review")
        current: dict[str, bytes | None] = {}
        for relative, record in file_records.items():
            value = _read_target_bytes(checkout, relative)
            current[relative] = value
            current_hash = _sha256(value)
            if current_hash not in {record.get("before_sha256"), record.get("desired_sha256")}:
                raise MaintenanceOperationError(f"{relative} changed after review; create a new setup plan")
        changed = [relative for relative, content in desired_files.items() if current[relative] != content]
        for relative in changed:
            _write_target_file(checkout, relative, desired_files[relative])
        after = {relative: _read_target_bytes(checkout, relative) for relative in desired_files}
        if any(after[relative] != content for relative, content in desired_files.items()):
            raise MaintenanceOperationError("maintenance files did not verify after writing")
    return {
        "status": "complete" if changed else "already_applied",
        "target": plan["target"],
        "plan_digest": plan["plan_digest"],
        "changed_paths": changed,
        "verified_hashes": {relative: _sha256(after[relative]) for relative in after},
    }


def setup(args: Any, home: Path, *, adapter_factory: Callable[[RepositoryTarget], Any] = _adapter) -> dict[str, Any]:
    applying = bool(getattr(args, "apply", False))
    if getattr(args, "all_public", False) and not getattr(args, "repository", None):
        if getattr(args, "authorize_plan", None) and not applying:
            raise MaintenanceOperationError("--authorize-plan requires --apply")
        return plan_all_public_setup(args, home, adapter_factory=adapter_factory)
    if applying:
        return apply_setup(args, home, adapter_factory=adapter_factory)
    if getattr(args, "authorize_plan", None):
        raise MaintenanceOperationError("--authorize-plan requires --apply")
    return plan_setup(args, home, adapter_factory=adapter_factory)

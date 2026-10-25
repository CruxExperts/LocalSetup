from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from typing import Any, Callable

from .adapter import GhCliAdapter, GitHubError
from .maintenance_config import (
    CONFIG_RELATIVE_PATH,
    WORKFLOW_RELATIVE_PATH,
    MaintenanceError,
    config_digest,
    parse_config_bytes,
    render_shared_workflow,
)
from .maintenance_discovery import (
    DiscoveryError,
    PublicRepository,
    checkout_for_repository,
    discover_checkouts,
    discover_public_repositories,
    read_origin,
)
from .model import RepositoryTarget
from .policy import PolicyError, parse_target


_GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_TARGET_FILE_LIMIT = 2 * 1024 * 1024
_TRACKED_STATUS_LIMIT = 1024 * 1024
_SENSITIVE_ENV = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE|API_KEY|ACCESS_KEY|AUTH|COOKIE)(?:_|$)", re.IGNORECASE)


class MaintenanceOperationError(RuntimeError):
    """A bounded maintenance operation failure safe to show to the user."""


def _adapter(target: RepositoryTarget) -> GhCliAdapter:
    return GhCliAdapter(target)


def _repo_metadata(adapter: Any, target: RepositoryTarget) -> dict[str, Any]:
    try:
        record = adapter.repository()
    except GitHubError as exc:
        raise MaintenanceOperationError("repository identity could not be read from GitHub") from exc
    repository_id = record.get("id") if isinstance(record, dict) else None
    full_name = record.get("full_name") if isinstance(record, dict) else None
    if type(repository_id) is not int or repository_id <= 0 or not isinstance(full_name, str) or full_name.count("/") != 1:
        raise MaintenanceOperationError("GitHub returned an invalid repository identity")
    if full_name.casefold() != target.full_name.casefold():
        raise MaintenanceOperationError("the requested repository name does not match GitHub's current identity")
    return record


def _assert_config_binding(config: dict[str, Any], target: RepositoryTarget, metadata: dict[str, Any]) -> None:
    if config["hostname"] != target.hostname:
        raise MaintenanceOperationError("maintenance config hostname does not match the selected GitHub host")
    if config["repository_id"] != metadata["id"]:
        raise MaintenanceOperationError("maintenance config immutable repository ID does not match GitHub")
    if config["full_name"] != metadata["full_name"]:
        raise MaintenanceOperationError("maintenance config full_name does not match GitHub")


def _path_for(checkout: Path, relative: str, *, create_parents: bool = False) -> Path:
    root = checkout.expanduser().resolve(strict=False)
    if not root.is_dir():
        raise MaintenanceOperationError("checkout must be an existing directory")
    pieces = PurePosixPath(relative).parts
    current = root
    for piece in pieces[:-1]:
        current = current / piece
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_parents:
                continue
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
            except OSError as exc:
                raise MaintenanceOperationError("checkout target directory could not be created") from exc
            info = current.lstat()
        except OSError as exc:
            raise MaintenanceOperationError("checkout target path could not be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MaintenanceOperationError("checkout target path contains a symlink or non-directory entry")
    target = root.joinpath(*pieces)
    try:
        target.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise MaintenanceOperationError("checkout target path escapes the repository") from exc
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise MaintenanceOperationError("checkout target file could not be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceOperationError("checkout target must be a regular single-link file")
    return target


def _read_target_bytes(checkout: Path, relative: str) -> bytes | None:
    path = _path_for(checkout, relative)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MaintenanceOperationError("checkout target file could not be read safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _TARGET_FILE_LIMIT:
            raise MaintenanceOperationError("checkout target file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _TARGET_FILE_LIMIT + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > _TARGET_FILE_LIMIT:
            raise MaintenanceOperationError("checkout target file exceeds the maintenance limit")
        return value
    finally:
        os.close(fd)


def _sha256(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _target_origin_matches(checkout: Path, target: RepositoryTarget) -> bool:
    origin = read_origin(checkout, target.hostname)
    return isinstance(origin, str) and origin.casefold() == target.full_name.casefold()


def _git_output(checkout: Path, argv: list[str], *, timeout: int = 5) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) > 4096:
        return None
    try:
        return result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None


def _local_head(checkout: Path) -> str | None:
    value = _git_output(checkout, ["rev-parse", "--verify", "HEAD^{commit}"])
    return value.lower() if isinstance(value, str) and _GIT_SHA.fullmatch(value.lower()) else None


def _tracked(checkout: Path, relative: str) -> bool:
    return _git_output(checkout, ["ls-files", "--error-unmatch", "--", relative]) == relative


def _unexpected_tracked_maintenance_files(checkout: Path) -> list[str] | None:
    value = _git_output(checkout, ["ls-files", "-z", "--", ".github/*maintenance*", ".github/workflows/*maintenance*"])
    if value is None:
        return None
    expected = {CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH}
    return sorted(path for path in value.split("\x00") if path and path not in expected)


def _signature_status(checkout: Path) -> str:
    value = _git_output(checkout, ["show", "-s", "--format=%G?", "HEAD"])
    if value == "G":
        return "cryptographically_valid"
    if value == "N":
        return "unsigned"
    if value in {"B", "E", "U", "X", "Y", "R"}:
        return "unknown_or_invalid"
    return "unknown"


def _tracked_input_state(checkout: Path) -> dict[str, Any]:
    """Summarize tracked checkout changes without returning any path names."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unknown", "changed_path_count": None, "digest": None}
    raw = result.stdout
    if result.returncode != 0 or len(raw) > _TRACKED_STATUS_LIMIT:
        return {"status": "unknown", "changed_path_count": None, "digest": None}
    if not raw:
        return {"status": "clean", "changed_path_count": 0, "digest": hashlib.sha256(b"").hexdigest()}
    if not raw.endswith(b"\x00"):
        return {"status": "unknown", "changed_path_count": None, "digest": None}
    entries = raw[:-1].split(b"\x00")
    if any(len(entry) < 4 or entry[2:3] != b" " for entry in entries):
        return {"status": "unknown", "changed_path_count": None, "digest": None}
    return {
        "status": "dirty",
        "changed_path_count": len(entries),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def _expected_files(checkout: Path, config: dict[str, Any]) -> dict[str, str]:
    actual_config = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
    if actual_config is None:
        config_state = "missing"
    else:
        try:
            parsed = parse_config_bytes(actual_config)
            config_state = "current" if parsed == config else "drift"
        except MaintenanceError:
            config_state = "drift"
    actual_workflow = _read_target_bytes(checkout, WORKFLOW_RELATIVE_PATH)
    if actual_workflow is None:
        workflow_state = "missing"
    else:
        workflow_state = "current" if actual_workflow == render_shared_workflow(config) else "drift"
    return {CONFIG_RELATIVE_PATH: config_state, WORKFLOW_RELATIVE_PATH: workflow_state}


def _load_tracked_config(checkout: Path) -> tuple[dict[str, Any] | None, str | None]:
    raw = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
    if raw is None:
        return None, "missing"
    try:
        return parse_config_bytes(raw), None
    except MaintenanceError:
        return None, "invalid"


def _tool_capabilities(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for adapter_name, commands in config["adapters"].items():
        executables = sorted({command["argv"][0] for command in commands})
        missing = [name for name in executables if shutil.which(name) is None]
        result[adapter_name] = {
            "status": "unknown" if missing else "supported",
            "executables": executables,
            "unavailable": missing,
        }
    return result


def _working_directory(checkout: Path, relative: str) -> Path | None:
    root = checkout.expanduser().resolve(strict=False)
    if relative == ".":
        return root if root.is_dir() else None
    try:
        normalized = PurePosixPath(relative)
        if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
            return None
        candidate = root.joinpath(*normalized.parts)
        candidate.resolve(strict=True).relative_to(root)
        cursor = root
        for part in normalized.parts:
            cursor = cursor / part
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
        return candidate if candidate.is_dir() else None
    except (OSError, ValueError):
        return None


def _run_validators(checkout: Path, config: dict[str, Any]) -> dict[str, Any]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_ENV.search(key) and not key.upper().startswith(("GH_", "GITHUB_"))
    }
    output: dict[str, Any] = {}
    for adapter_name, commands in config["adapters"].items():
        for command in commands:
            name = command["name"]
            executable = command["argv"][0]
            executable_path = shutil.which(executable)
            if executable_path is None:
                output[f"{adapter_name}/{name}"] = {"status": "unknown", "reason": "executable_unavailable"}
                continue
            workdir = _working_directory(checkout, command["cwd"])
            if workdir is None:
                output[f"{adapter_name}/{name}"] = {"status": "unknown", "reason": "working_directory_unavailable"}
                continue
            try:
                result = subprocess.run(
                    list(command["argv"]),
                    cwd=workdir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=command["timeout_seconds"],
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                output[f"{adapter_name}/{name}"] = {"status": "failed", "reason": "timeout"}
            except OSError:
                output[f"{adapter_name}/{name}"] = {"status": "unknown", "reason": "execution_unavailable"}
            else:
                output[f"{adapter_name}/{name}"] = {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_code": int(result.returncode),
                }
    return output


def _verify_one(
    repository: PublicRepository | None,
    *,
    target: RepositoryTarget,
    checkout: Path,
    adapter_factory: Callable[[RepositoryTarget], Any],
    validate: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target": {"hostname": target.hostname, "full_name": target.full_name},
        "checkout": str(checkout),
        "config": "unknown",
        "workflow": "unknown",
        "origin": "unknown",
        "tool_capabilities": {},
        "capability_status": "unknown",
        "security_advisories": {"status": "unknown", "counts": {"dependabot": None, "code_scanning": None, "secret_scanning": None}},
        "ci_evidence": {"status": "unknown", "ok": False},
        "tracked_input_state": _tracked_input_state(checkout),
    }
    adapter = adapter_factory(target)
    metadata = _repo_metadata(adapter, target)
    if repository is not None and repository.repository_id != metadata["id"]:
        raise MaintenanceOperationError("public discovery and repository lookup returned different immutable IDs")
    result["target"].update(repository_id=metadata["id"], full_name=metadata["full_name"])
    result["origin"] = "current" if _target_origin_matches(checkout, target) else "drift"
    config, error = _load_tracked_config(checkout)
    if error:
        result["config"] = error
        result["config_tracked"] = False
        result["workflow"] = "unknown"
        result["handoff_ready"] = False
        return result
    assert config is not None
    try:
        _assert_config_binding(config, target, metadata)
    except MaintenanceOperationError:
        result["config"] = "identity_drift"
        result["config_tracked"] = _tracked(checkout, CONFIG_RELATIVE_PATH)
        result["workflow"] = "unknown"
        result["handoff_ready"] = False
        return result
    result["config"] = "current"
    result["config_digest"] = config_digest(config)
    result["config_tracked"] = _tracked(checkout, CONFIG_RELATIVE_PATH)
    if not result["config_tracked"]:
        result["config"] = "untracked"
    file_state = _expected_files(checkout, config)
    result["workflow"] = file_state[WORKFLOW_RELATIVE_PATH]
    result["workflow_tracked"] = _tracked(checkout, WORKFLOW_RELATIVE_PATH)
    if not result["workflow_tracked"] and result["workflow"] == "current":
        result["workflow"] = "untracked"
    result["tool_capabilities"] = _tool_capabilities(config)
    result["capability_status"] = "unknown" if any(item["status"] != "supported" for item in result["tool_capabilities"].values()) else "supported"
    result["security_advisories"] = _security_advisory_counts(adapter)
    unexpected = _unexpected_tracked_maintenance_files(checkout)
    result["unexpected_tracked_maintenance_files"] = None if unexpected is None else len(unexpected)
    head = _local_head(checkout)
    branch = metadata.get("default_branch")
    remote_head = None
    if isinstance(branch, str) and branch:
        try:
            remote_head = adapter.branch_head_sha(branch).lower()
        except GitHubError:
            remote_head = None
    result["head"] = {"local": head, "remote_default": remote_head, "status": "current" if head and remote_head and head == remote_head else "stale_or_unknown"}
    result["ci_evidence"] = _ci_evidence(target.full_name, head, tuple(config["validation_owners"]))
    result["commit_signature"] = _signature_status(checkout)
    validation_inputs_stable = True
    if validate:
        # Validators may be generators or formatters. Snapshot the exact files
        # and Git evidence whose earlier reads justified the handoff decision.
        before_config = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
        before_workflow = _read_target_bytes(checkout, WORKFLOW_RELATIVE_PATH)
        before_tracked_state = result["tracked_input_state"]
        result["local_validation"] = _run_validators(checkout, config)
        result["local_validation_sandbox"] = "none; commands ran directly with shell disabled and sensitive environment tokens removed"
        try:
            after_config = _read_target_bytes(checkout, CONFIG_RELATIVE_PATH)
            after_workflow = _read_target_bytes(checkout, WORKFLOW_RELATIVE_PATH)
        except MaintenanceOperationError:
            after_config = after_workflow = None
        after_head = _local_head(checkout)
        result["tracked_input_state"] = _tracked_input_state(checkout)
        result["head"] = {
            "local": after_head,
            "remote_default": remote_head,
            "status": "current" if after_head and remote_head and after_head == remote_head else "stale_or_unknown",
        }
        result["origin"] = "current" if _target_origin_matches(checkout, target) else "drift"
        result["config_tracked"] = _tracked(checkout, CONFIG_RELATIVE_PATH)
        result["workflow_tracked"] = _tracked(checkout, WORKFLOW_RELATIVE_PATH)
        after_unexpected = _unexpected_tracked_maintenance_files(checkout)
        result["unexpected_tracked_maintenance_files"] = None if after_unexpected is None else len(after_unexpected)
        if after_config != before_config or after_config is None:
            result["config"] = "changed_during_validation"
            result.pop("config_digest", None)
        if after_workflow != before_workflow or after_workflow is None:
            result["workflow"] = "changed_during_validation"
        if after_head != head:
            result["ci_evidence"] = {"status": "unknown", "ok": False}
            result["commit_signature"] = _signature_status(checkout)
        validation_inputs_stable = (
            head is not None and after_head == head
            and before_config is not None and after_config == before_config
            and before_workflow is not None and after_workflow == before_workflow
            and before_tracked_state == result["tracked_input_state"]
            and result["tracked_input_state"]["status"] == "clean"
        )
        result["validation_input_stability"] = "stable" if validation_inputs_stable else "changed_or_unknown"
    handoff_ready = (
        result["config"] == "current"
        and result["config_tracked"]
        and result["workflow"] == "current"
        and result["workflow_tracked"]
        and result["origin"] == "current"
        and result["head"]["status"] == "current"
        and result["ci_evidence"]["ok"] is True
        and result["tracked_input_state"]["status"] == "clean"
        and result["commit_signature"] == "cryptographically_valid"
        and all(item["status"] == "supported" for item in result["tool_capabilities"].values())
        and result["security_advisories"]["status"] == "complete"
        and result["unexpected_tracked_maintenance_files"] == 0
    )
    if validate:
        handoff_ready = handoff_ready and validation_inputs_stable and all(item["status"] == "passed" for item in result["local_validation"].values())
    result["handoff_ready"] = bool(handoff_ready)
    return result


def _select_targets(args: Any, *, adapter_factory: Callable[[RepositoryTarget], Any]) -> list[tuple[PublicRepository | None, RepositoryTarget, Path | None]]:
    hostname = getattr(args, "hostname", "github.com")
    checkout_arg = getattr(args, "checkout", None)
    all_public = bool(getattr(args, "all_public", False))
    if not all_public:
        repository_name = getattr(args, "repository", None)
        if not repository_name:
            raise MaintenanceOperationError("--repository OWNER/REPO is required unless --all-public is selected")
        try:
            target = parse_target(hostname, repository_name)
        except PolicyError as exc:
            raise MaintenanceOperationError(str(exc)) from exc
        if checkout_arg is None:
            raise MaintenanceOperationError("--checkout PATH is required for a single repository")
        checkout = Path(checkout_arg).expanduser().resolve(strict=False)
        return [(None, target, checkout)]
    owner = getattr(args, "owner", None)
    root_arg = getattr(args, "checkout_root", None)
    if not owner or not root_arg:
        raise MaintenanceOperationError("--all-public requires --owner OWNER and --checkout-root ROOT")
    try:
        repositories = discover_public_repositories(hostname, owner, adapter_factory=adapter_factory)
        checkout_map = discover_checkouts(Path(root_arg), hostname)
    except DiscoveryError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    selected: list[tuple[PublicRepository | None, RepositoryTarget, Path | None]] = []
    for repository in repositories:
        checkout = checkout_for_repository(checkout_map, repository.full_name)
        if checkout is None:
            target = parse_target(hostname, repository.full_name)
            selected.append((repository, target, None))
            continue
        target = parse_target(hostname, repository.full_name)
        selected.append((repository, target, checkout))
    return selected


def verify(args: Any, home: Path, *, adapter_factory: Callable[[RepositoryTarget], Any] = _adapter) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for repository, target, checkout in _select_targets(args, adapter_factory=adapter_factory):
        if repository is not None and checkout is None:
            rows.append({
                "target": {"hostname": target.hostname, "repository_id": repository.repository_id, "full_name": repository.full_name},
                "status": "checkout_missing_or_ambiguous",
                "handoff_ready": False,
                "config": "unknown",
                "workflow": "unknown",
                "tool_capabilities": {},
                "capability_status": "unknown",
                "security_advisories": {"status": "unknown", "counts": {"dependabot": None, "code_scanning": None, "secret_scanning": None}},
                "ci_evidence": {"status": "unknown", "ok": False},
            })
            continue
        assert checkout is not None
        rows.append(_verify_one(repository, target=target, checkout=checkout, adapter_factory=adapter_factory, validate=bool(getattr(args, "validate", False))))
    ready = bool(rows) and all(row.get("handoff_ready") is True for row in rows)
    return {
        "status": "verified" if ready else "incomplete",
        "scope": "all_public" if getattr(args, "all_public", False) else "repository",
        "owner": getattr(args, "owner", None),
        "repository_count": len(rows),
        "handoff_ready": ready,
        "repositories": rows,
    }


def _security_advisory_counts(adapter: Any) -> dict[str, Any]:
    counts: dict[str, int | None] = {}
    for label, method_name in (
        ("dependabot", "dependabot_alert_count"),
        ("code_scanning", "code_scanning_alert_count"),
        ("secret_scanning", "secret_scanning_alert_count"),
    ):
        method = getattr(adapter, method_name, None)
        if not callable(method):
            counts[label] = None
            continue
        try:
            value = method()
        except GitHubError:
            value = None
        if type(value) is not int or value < 0:
            counts[label] = None
        else:
            counts[label] = value
    return {"status": "complete" if all(value is not None for value in counts.values()) else "unknown", "counts": counts}


def _ci_evidence(repository: str, sha: str | None, workflows: tuple[str, ...]) -> dict[str, Any]:
    if not sha or not workflows:
        return {"status": "unknown", "ok": False}
    from .ci_evidence import check_ci_evidence

    evidence = check_ci_evidence(repository, sha, workflows=workflows, timeout_seconds=15)
    if not isinstance(evidence, dict) or evidence.get("ok") is not True:
        return {"status": "unknown", "ok": False}
    runs = evidence.get("runs")
    if not isinstance(runs, list) or len(runs) != len(workflows) or any(not isinstance(run, dict) for run in runs):
        return {"status": "unknown", "ok": False}
    if any(run.get("event") == "push" for run in runs):
        return {"status": "partial", "ok": False}
    if any(run.get("event") not in {"pull_request", "merge_group", "workflow_dispatch"} for run in runs):
        return {"status": "unknown", "ok": False}
    if any(run.get("workflow") != workflow for run, workflow in zip(runs, workflows, strict=True)):
        return {"status": "unknown", "ok": False}
    return {"status": "verified", "ok": True}

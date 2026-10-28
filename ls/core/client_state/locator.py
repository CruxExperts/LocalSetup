from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import hashlib
import json
from collections.abc import Mapping

from ..client_registry import ClientRegistryError, load_client_registry
from .models import ClientStateError, GitContext, StateLocation


_NOT_REPOSITORY = ("not a git repository", "outside repository", "invalid gitfile format")
_STATE_CHILD = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=git_environment(),
    )


def _git_bytes(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        env=git_environment(),
    )


def _required_git(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result.returncode != 0:
        raise ClientStateError("Git state probe failed", code="git_probe_failed")
    value = result.stdout.strip()
    if not value:
        raise ClientStateError("Git state probe returned no value", code="git_probe_failed")
    return value


def _required_git_path(cwd: Path, *args: str) -> str:
    result = _git_bytes(cwd, *args)
    if result.returncode != 0:
        raise ClientStateError("Git state probe failed", code="git_probe_failed")
    value = result.stdout
    if not value.endswith(b"\n"):
        raise ClientStateError("Git state probe returned an invalid path", code="git_probe_failed")
    value = value[:-1]
    if not value or b"\x00" in value or b"\n" in value or b"\r" in value:
        raise ClientStateError("Git state probe returned an invalid path", code="git_probe_failed")
    decoded = os.fsdecode(value)
    if not Path(decoded).is_absolute():
        raise ClientStateError("Git state probe returned an invalid path", code="git_probe_failed")
    return decoded


def _resolved_git_root(cwd: Path) -> Path:
    try:
        root = Path(_required_git_path(cwd, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError
    except ClientStateError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ClientStateError("Git state probe returned an invalid path", code="git_probe_failed") from exc
    return root


def _resolve_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError
    except (OSError, RuntimeError) as exc:
        raise ClientStateError(
            "state probe directory is unavailable", code="invalid_directory"
        ) from exc
    return resolved


def _git_discovery_device(path: Path) -> int:
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ClientStateError(
            "Git discovery boundary is unavailable", code="git_probe_failed"
        ) from exc
    if not stat.S_ISDIR(details.st_mode):
        raise ClientStateError(
            "Git discovery boundary is invalid", code="git_probe_failed"
        )
    return details.st_dev


def _has_git_marker(cwd: Path) -> bool:
    current = cwd
    current_device = _git_discovery_device(current)
    while True:
        marker = current / ".git"
        try:
            marker.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ClientStateError(
                "Git state marker is unavailable", code="git_probe_failed"
            ) from exc
        else:
            return True
        parent = current.parent
        if parent == current:
            return False
        parent_device = _git_discovery_device(parent)
        if parent_device != current_device:
            return False
        current = parent
        current_device = parent_device


def probe_git_context(cwd: Path) -> GitContext | None:
    cwd = _resolve_directory(cwd)
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        detail = (inside.stderr or inside.stdout).lower()
        if any(marker in detail for marker in _NOT_REPOSITORY):
            if _has_git_marker(cwd):
                raise ClientStateError(
                    "Git worktree marker is invalid", code="git_probe_failed"
                )
            return None
        raise ClientStateError("ambiguous Git worktree probe failure", code="git_probe_failed")
    if inside.stdout.strip() != "true":
        bare = _git(cwd, "rev-parse", "--is-bare-repository")
        if bare.returncode == 0 and bare.stdout.strip() == "true":
            raise ClientStateError("bare repositories do not have repo-scoped client state")
        raise ClientStateError("Git reports a repository but not a supported worktree")

    root = _resolved_git_root(cwd)
    exclude = Path(
        _required_git_path(cwd, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    )
    head_result = _git(cwd, "rev-parse", "--verify", "HEAD")
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    ref_result = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None
    return GitContext(root=root, exclude_path=exclude, head=head, ref=ref)


def _assert_safe_state_path(anchor: Path, target: Path) -> None:
    if not anchor.is_absolute() or not target.is_absolute():
        raise ClientStateError("client state path must be absolute", code="unsafe_state_path")
    try:
        target.relative_to(anchor)
    except ValueError as exc:
        raise ClientStateError("client state escapes its owning root", code="unsafe_state_path") from exc
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current /= part
        try:
            details = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ClientStateError(
                "client state path is unsafe or unavailable", code="unsafe_state_path"
            ) from exc
        if stat.S_ISLNK(details.st_mode):
            raise ClientStateError("client state path must not traverse a symlink", code="unsafe_state_path")
        if not stat.S_ISDIR(details.st_mode):
            raise ClientStateError(
                "client state path components must be directories", code="unsafe_state_path"
            )


def _valid_global_managed_directory(details: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.geteuid()
        and details.st_mode & 0o022 == 0
    )


def _valid_global_pre_owner_directory(details: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid in {0, os.geteuid()}
        and (details.st_mode & 0o022 == 0 or details.st_mode & stat.S_ISVTX != 0)
    )


def _assert_global_ownership(owner: Path, target: Path) -> None:
    try:
        relative = target.relative_to(owner)
    except ValueError as exc:
        raise ClientStateError("client state escapes its owning root", code="unsafe_state_path") from exc
    managed = (owner, *(owner / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
    owner_exists = False
    for index, current in enumerate(managed):
        try:
            details = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ClientStateError(
                "global client state ownership is unavailable", code="unsafe_state_path"
            ) from exc
        if index == 0:
            owner_exists = True
        if not _valid_global_managed_directory(details):
            raise ClientStateError(
                "global client state path has unsafe ownership or permissions",
                code="unsafe_state_path",
            )
    if owner_exists:
        ancestor = owner.parent
        try:
            details = ancestor.stat(follow_symlinks=False)
        except OSError as exc:
            raise ClientStateError(
                "global client state ownership is unavailable", code="unsafe_state_path"
            ) from exc
        if not _valid_global_pre_owner_directory(details):
            raise ClientStateError(
                "global client state ancestor has unsafe ownership or permissions",
                code="unsafe_state_path",
            )
        return
    ancestor = owner.parent
    while True:
        try:
            details = ancestor.stat(follow_symlinks=False)
            break
        except FileNotFoundError:
            parent = ancestor.parent
            if parent == ancestor:
                raise ClientStateError(
                    "global client state ownership is unavailable", code="unsafe_state_path"
                )
            ancestor = parent
        except OSError as exc:
            raise ClientStateError(
                "global client state ownership is unavailable", code="unsafe_state_path"
            ) from exc
    if not _valid_global_pre_owner_directory(details):
        raise ClientStateError(
            "global client state ancestor has unsafe ownership or permissions",
            code="unsafe_state_path",
        )


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClientStateError(
            "client state path is unsafe or unavailable", code="unsafe_state_path"
        ) from exc
    return (result.st_dev, result.st_ino)


def _child(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _STATE_CHILD.fullmatch(value):
        raise ClientStateError(
            "state child must be lowercase kebab-case", code="invalid_state_child"
        )
    return value


def _global_path(raw: str, *, home: Path) -> tuple[Path, str, Path]:
    if raw == "$CODEX_HOME" or raw.startswith("$CODEX_HOME/"):
        configured = os.environ.get("CODEX_HOME")
        if configured is None:
            candidate = home / ".codex"
        else:
            if not configured.strip():
                raise ClientStateError(
                    "CODEX_HOME must be a non-empty absolute path",
                    code="invalid_environment",
                )
            try:
                candidate = Path(configured).expanduser()
            except RuntimeError as exc:
                raise ClientStateError(
                    "CODEX_HOME must be a non-empty absolute path",
                    code="invalid_environment",
                ) from exc
            if not candidate.is_absolute():
                raise ClientStateError(
                    "CODEX_HOME must be a non-empty absolute path",
                    code="invalid_environment",
                )
        owner = Path(os.path.abspath(candidate))
        suffix = raw.removeprefix("$CODEX_HOME").lstrip("/")
        target = owner / suffix
    elif raw == "~" or raw.startswith("~/"):
        owner = Path(os.path.abspath(home))
        target = home / raw.removeprefix("~/") if raw != "~" else home
    else:
        raise ClientStateError("global client state must be home- or CODEX_HOME-scoped")
    target = Path(os.path.abspath(target))
    _assert_safe_state_path(owner, target)
    _assert_global_ownership(owner, target)
    return target, raw, owner


def _plain(value):
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _variant(repo_root: Path, key: str):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*", key):
        raise ClientStateError("client must be a canonical family/variant key", code="invalid_client")
    family, variant = key.split("/", 1)
    try:
        registry = load_client_registry(repo_root)
        row = registry.variant(family, variant)
        snapshot = {
            "key": row.key,
            "registry_schema_version": registry.schema_version,
            "variant": _plain(row.data),
        }
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return registry, row, digest
    except (KeyError, ClientRegistryError) as exc:
        raise ClientStateError("unknown or invalid client variant", code="invalid_client") from exc


def resolve_state_location(
    repo_root: Path,
    client: str,
    *,
    cwd: Path,
    home: Path,
    scope: str = "auto",
    child: str | None = None,
) -> StateLocation:
    if scope not in {"auto", "repo", "global"}:
        raise ClientStateError(f"unsupported state scope: {scope}")
    child = _child(child)
    repo_root = repo_root.resolve(strict=True)
    cwd = repo_root if scope == "global" else _resolve_directory(cwd)
    home = home.expanduser().resolve()
    registry, variant, variant_digest = _variant(repo_root, client)
    git = None if scope == "global" else probe_git_context(cwd)
    selected = "repo" if git is not None else "global"
    if scope == "repo" and git is None:
        raise ClientStateError("repo scope requested outside a Git worktree")
    if scope == "global":
        selected = "global"

    state = variant.data["state"][selected]
    if state["status"] != "supported" or not state["path"]:
        raise ClientStateError(f"{client} has no supported {selected} state root")
    raw = str(state["path"])
    if selected == "repo":
        assert git is not None
        parent_root = Path(os.path.abspath(git.root / raw))
        _assert_safe_state_path(git.root, parent_root)
        parent_display = parent_root.relative_to(git.root).as_posix()
        owner_root = git.root
    else:
        parent_root, parent_display, owner_root = _global_path(raw, home=home)
        git = None
    root = parent_root / child if child else parent_root
    display = f"{parent_display}/{child}" if child else parent_display
    _assert_safe_state_path(owner_root, root)
    if selected == "global":
        _assert_global_ownership(owner_root, root)
    return StateLocation(
        client=client,
        scope=selected,
        requested_scope=scope,
        repo_root=repo_root,
        cwd=cwd,
        home=home,
        owner_root=owner_root,
        owner_identity=_identity(owner_root),
        parent_root=parent_root,
        parent_identity=_identity(parent_root),
        root=root,
        root_identity=_identity(root),
        state_path=display,
        child=child,
        git=git,
        registry_schema_version=registry.schema_version,
        variant_digest=variant_digest,
    )


def refresh_state_location(location: StateLocation, *, allow_created_roots: bool = False) -> StateLocation:
    current = resolve_state_location(
        location.repo_root,
        location.client,
        cwd=location.cwd,
        home=location.home,
        scope=location.requested_scope,
        child=location.child,
    )
    stable = (
        "client", "scope", "owner_root", "parent_root", "root", "state_path", "child",
        "registry_schema_version", "variant_digest",
    )
    identities_match = (
        current.owner_identity == location.owner_identity
        and current.parent_identity == location.parent_identity
        and current.root_identity == location.root_identity
    )
    if allow_created_roots:
        identities_match = (
            (
                current.owner_identity == location.owner_identity
                or (location.owner_identity is None and current.owner_identity is not None)
            )
            and (
                current.parent_identity == location.parent_identity
                or (location.parent_identity is None and current.parent_identity is not None)
            )
            and (
                current.root_identity == location.root_identity
                or (location.root_identity is None and current.root_identity is not None)
            )
        )
    if (
        any(getattr(current, field) != getattr(location, field) for field in stable)
        or current.git != location.git
        or not identities_match
    ):
        raise ClientStateError("client state binding is stale", code="stale_state_binding")
    return current

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .adapter import GhCliAdapter, GitHubError
from .model import RepositoryTarget


_PAGE_SIZE = 100
_PAGE_LIMIT = 100
_MAX_CHECKOUT_DIRECTORIES = 5000
_REMOTE_PATH = re.compile(r"^/?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})/?$")
_SSH_REMOTE = re.compile(r"^git@([^:]+):(.+)$", re.IGNORECASE)


class DiscoveryError(RuntimeError):
    """A bounded GitHub or checkout discovery error without remote response text."""


@dataclass(frozen=True)
class PublicRepository:
    repository_id: int
    full_name: str
    default_branch: str | None


def _repository_target(hostname: str, owner: str) -> RepositoryTarget:
    if not isinstance(owner, str) or not owner or "/" in owner or "\\" in owner or "\x00" in owner:
        raise DiscoveryError("owner must be a GitHub login")
    return RepositoryTarget(hostname=hostname, owner=owner, repository="_discovery")


def discover_public_repositories(
    hostname: str,
    owner: str,
    *,
    adapter_factory: Callable[[RepositoryTarget], Any] = GhCliAdapter,
) -> list[PublicRepository]:
    """Fully page the public repositories for a GitHub user or organization."""
    adapter = adapter_factory(_repository_target(hostname, owner))
    encoded_owner = quote(owner, safe="")
    try:
        owner_record = adapter._request("GET", f"users/{encoded_owner}", operation="maintenance_owner_identity")
    except GitHubError as exc:
        raise DiscoveryError("GitHub owner identity could not be read") from exc
    if (
        not isinstance(owner_record, dict)
        or not isinstance(owner_record.get("login"), str)
        or owner_record["login"].casefold() != owner.casefold()
        or owner_record.get("type") not in {"User", "Organization"}
    ):
        raise DiscoveryError("GitHub owner type is unknown")
    collection = "users" if owner_record["type"] == "User" else "orgs"
    endpoint_prefix = f"{collection}/{encoded_owner}/repos?type=public&sort=full_name"
    discovered: list[PublicRepository] = []
    seen_ids: set[int] = set()
    for page in range(1, _PAGE_LIMIT + 1):
        try:
            rows = adapter._request(
                "GET",
                f"{endpoint_prefix}&per_page={_PAGE_SIZE}&page={page}",
                operation="maintenance_public_repository_discovery",
            )
        except GitHubError as exc:
            raise DiscoveryError("public repository discovery failed") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or len(rows) > _PAGE_SIZE:
            raise DiscoveryError("GitHub returned an invalid public repository page")
        for row in rows:
            repo_id = row.get("id")
            full_name = row.get("full_name")
            private = row.get("private")
            default_branch = row.get("default_branch")
            if (
                type(repo_id) is not int
                or repo_id <= 0
                or repo_id in seen_ids
                or not isinstance(full_name, str)
                or full_name.count("/") != 1
                or full_name.split("/", 1)[0].casefold() != owner.casefold()
                or private is not False
                or (default_branch is not None and not isinstance(default_branch, str))
            ):
                raise DiscoveryError("GitHub returned an invalid or duplicate public repository identity")
            seen_ids.add(repo_id)
            discovered.append(PublicRepository(repo_id, full_name, default_branch))
        if len(rows) < _PAGE_SIZE:
            return sorted(discovered, key=lambda item: (item.full_name.casefold(), item.repository_id))
    raise DiscoveryError("public repository discovery exceeded its pagination limit")


def origin_repository(remote: str, hostname: str) -> str | None:
    """Return OWNER/REPO for a credential-free GitHub origin URL, or ``None``."""
    if not isinstance(remote, str) or not remote or "\x00" in remote:
        return None
    ssh = _SSH_REMOTE.fullmatch(remote)
    if ssh:
        host, path = ssh.groups()
        if host.casefold() != hostname.casefold():
            return None
        match = _REMOTE_PATH.fullmatch(_without_git_suffix(path))
    else:
        try:
            parsed = urlsplit(remote)
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.casefold()
        credentialed_ssh = scheme == "ssh" and parsed.username == "git" and parsed.password is None
        if (
            scheme not in {"https", "ssh"}
            or parsed.hostname is None
            or parsed.hostname.casefold() != hostname.casefold()
            or (parsed.username is not None and not credentialed_ssh)
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        match = _REMOTE_PATH.fullmatch(_without_git_suffix(parsed.path))
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _without_git_suffix(path: str) -> str:
    value = path.rstrip("/")
    return value[:-4] if value.casefold().endswith(".git") else value


def read_origin(checkout: Path, hostname: str) -> str | None:
    """Read only the named ``origin`` remote; upstream and other remotes are ignored."""
    root = checkout.expanduser().resolve(strict=False)
    if not root.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout) > 4096:
        return None
    try:
        remote = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    return origin_repository(remote, hostname)


def discover_checkouts(root: Path, hostname: str) -> dict[str, list[Path]]:
    """Find origin remotes below a bounded root; never inspect ``upstream``."""
    checkout_root = root.expanduser().resolve(strict=False)
    if not checkout_root.is_dir():
        raise DiscoveryError("checkout root must be an existing directory")
    found: dict[str, list[Path]] = {}
    visited = 0
    for directory, child_dirs, files in os.walk(checkout_root, topdown=True, followlinks=False):
        current = Path(directory)
        child_dirs[:] = [name for name in child_dirs if name not in {".git", "node_modules", ".venv", "venv", "vendor", "target"}]
        visited += 1
        if visited > _MAX_CHECKOUT_DIRECTORIES:
            raise DiscoveryError("checkout discovery exceeded its directory limit")
        if (current / ".git").is_dir() or ".git" in files:
            full_name = read_origin(current, hostname)
            if full_name is not None:
                found.setdefault(full_name.casefold(), []).append(current)
            child_dirs[:] = []
    for paths in found.values():
        paths.sort(key=lambda item: item.as_posix().casefold())
    return found


def checkout_for_repository(checkout_map: dict[str, list[Path]], full_name: str) -> Path | None:
    matches = checkout_map.get(full_name.casefold(), [])
    return matches[0] if len(matches) == 1 else None

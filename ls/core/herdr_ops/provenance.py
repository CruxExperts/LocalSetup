"""Offline Herdr source pins and read-only upstream freshness checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse


LATEST_URL = "https://herdr.dev/latest.json"
GITHUB_LATEST_URL = "https://api.github.com/repos/herdrdev/herdr/releases/latest"
GITHUB_SOURCE_URL = "https://api.github.com/repos/herdrdev/herdr/commits/"
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 8.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def default_manifest_path(root: Path) -> Path:
    return (
        root
        / "ls"
        / "skills"
        / "ls-herdr"
        / "references"
        / "upstream"
        / "provenance.json"
    )


def check_provenance(
    root: Path,
    manifest_path: Path | None = None,
    *,
    fetch_latest: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    latest_fetcher: Callable[[float], tuple[str, dict[str, Any]]] | None = None,
    source_fetcher: Callable[[dict[str, Any], float], tuple[str, dict[str, str]]] | None = None,
    server_endpoint: str | None = None,
    binary_path: Path | None = None,
) -> dict[str, Any]:
    """Check the reviewed manifest without modifying files or host state."""
    path = manifest_path or default_manifest_path(root)
    manifest, manifest_issue = _read_manifest(path)
    if manifest is None:
        unknown = _category("unknown", [manifest_issue or "manifest is unavailable"])
        return {
            "manifest": _manifest_label(path, root),
            "source": unknown,
            "schema": unknown,
            "binary": unknown,
            "server": _category("unknown", ["no compatible manifest is available"]),
            "upstream": _category("unknown", ["manifest is unavailable"]),
            "source_watch": _category("unknown", ["manifest is unavailable"]),
        }

    source, schema = _check_source_and_schema(path, manifest)
    binary = _check_binary_pins(manifest, binary_path)
    server = _check_server(server_endpoint, manifest)
    upstream = _category("not_checked", ["network check was not requested"])
    source_watch = _category("not_checked", ["network check was not requested"])
    if fetch_latest:
        fetcher = latest_fetcher or fetch_latest_metadata
        try:
            source_name, latest = fetcher(timeout)
            upstream = _compare_latest(manifest, source_name, latest)
        except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
            message = _safe_error(exc)
            upstream = _category("unknown", [message])
        try:
            revision, hashes = (source_fetcher or fetch_source_hashes)(manifest, timeout)
            source_watch = _compare_source_watch(manifest, revision, hashes)
        except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
            source_watch = _category("unknown", [_safe_error(exc)])

    return {
        "manifest": _manifest_label(path, root),
        "source": source,
        "schema": schema,
        "binary": binary,
        "server": server,
        "upstream": upstream,
        "source_watch": source_watch,
    }


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("provenance", help="Check Herdr source pins and upstream freshness")
    commands = parser.add_subparsers(dest="provenance_command", required=True)
    check = commands.add_parser("check", help="Validate local pins; network lookup is opt-in")
    check.add_argument("--manifest", type=Path, default=None)
    check.add_argument("--network", action="store_true", help="Read official latest release metadata")
    check.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    check.add_argument("--endpoint", default=None, help="Optional Herdr Unix socket for a server probe")
    check.add_argument("--binary", type=Path, default=None, help="Optional local Herdr binary to hash")

    watch = commands.add_parser("watch", help="Read official release metadata for a daily report")
    watch.add_argument("--manifest", type=Path, default=None)
    watch.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    watch.add_argument("--endpoint", default=None)
    watch.add_argument("--binary", type=Path, default=None)


def dispatch(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    command = getattr(args, "provenance_command", None)
    manifest_path = getattr(args, "manifest", None)
    if manifest_path is not None and not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    if command not in {"check", "watch"}:
        raise ValueError("unknown provenance command")
    if command == "watch" and getattr(args, "network", False):
        raise ValueError("watch always checks latest metadata")
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be between 0 and 60 seconds")
    return check_provenance(
        root,
        manifest_path,
        fetch_latest=command == "watch" or bool(getattr(args, "network", False)),
        timeout=float(timeout),
        server_endpoint=getattr(args, "endpoint", None),
        binary_path=getattr(args, "binary", None),
    )


def fetch_latest_metadata(timeout: float) -> tuple[str, dict[str, Any]]:
    """Fetch only official, bounded release metadata; latest.json may be blocked."""
    try:
        payload = _get_json(LATEST_URL, timeout, github=False)
        if not isinstance(payload, dict):
            raise ValueError("latest metadata is not a JSON object")
        return "herdr.dev/latest.json", payload
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        payload = _get_json(GITHUB_LATEST_URL, timeout, github=True)
        if not isinstance(payload, dict):
            raise ValueError("GitHub release metadata is not a JSON object")
        if not isinstance(payload.get("tag_name"), str):
            raise ValueError("GitHub latest release metadata has no tag")
        return "GitHub latest release API", payload


def _get_json(url: str, timeout: float, *, github: bool) -> dict[str, Any]:
    headers = {"User-Agent": "LocalSetup-Herdr-Provenance/1", "Accept": "application/json"}
    if github:
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = urlparse(response.geturl())
        allowed = {"herdr.dev", "api.github.com", "github.com"}
        if final.scheme != "https" or final.hostname not in allowed:
            raise ValueError("official metadata redirected to an unapproved host")
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("official metadata exceeds the 1 MiB limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("official metadata is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("official metadata is not an object")
    return value


def fetch_source_hashes(manifest: dict[str, Any], timeout: float) -> tuple[str, dict[str, str]]:
    """Read bounded official source bytes at one immutable upstream revision."""
    watch = manifest.get("source_watch")
    files = watch.get("files") if isinstance(watch, dict) else None
    if watch is None or not isinstance(files, list) or not files or len(files) > 16 or watch.get("ref") not in {"main", "master"}:
        raise ValueError("source watch pins are unavailable or unsupported")
    latest = _get_json(GITHUB_SOURCE_URL + watch["ref"], timeout, github=True)
    revision = latest.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("official source revision is unavailable")
    hashes: dict[str, str] = {}
    for item in files:
        source_path = item.get("source_path") if isinstance(item, dict) else None
        if not isinstance(source_path, str) or not _safe_relative_path(source_path) or source_path in hashes:
            raise ValueError("source watch contains an invalid or duplicate path")
        url = f"https://raw.githubusercontent.com/herdrdev/herdr/{revision}/{source_path}"
        request = urllib.request.Request(url, headers={"User-Agent": "LocalSetup-Herdr-Provenance/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != "raw.githubusercontent.com":
                raise ValueError("official source redirected to an unapproved host")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("official source file exceeds the 1 MiB limit")
        hashes[source_path] = hashlib.sha256(payload).hexdigest()
    return revision, hashes


def _compare_source_watch(manifest: dict[str, Any], revision: str, hashes: dict[str, str]) -> dict[str, Any]:
    watch = manifest.get("source_watch")
    files = watch.get("files") if isinstance(watch, dict) else None
    if not isinstance(files, list) or not files:
        return _category("unknown", ["source watch pins are unavailable"])
    findings: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("source_path"), str) or not isinstance(item.get("sha256"), str):
            return _category("unknown", ["source watch pin is invalid"])
        if hashes.get(item["source_path"]) is None:
            return _category("unknown", [f"source observation is missing: {item['source_path']}"])
        if hashes[item["source_path"]] != item["sha256"]:
            findings.append(f"upstream source changed: {item['source_path']}")
    return _category("drift" if findings else "current", findings or [f"source files match pins at {revision}"])


def _read_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "provenance manifest is missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "provenance manifest cannot be read as JSON"
    if not isinstance(value, dict):
        return None, "provenance manifest must be a JSON object"
    if value.get("schema_version") != 1:
        return None, "unsupported provenance manifest schema_version"
    source = value.get("source")
    release = value.get("release")
    files = value.get("upstream_files")
    if not isinstance(source, dict) or not isinstance(release, dict) or not isinstance(files, list):
        return None, "provenance manifest is missing source, release, or upstream_files"
    return value, None


def _check_source_and_schema(path: Path, manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source_findings: list[str] = []
    schema_findings: list[str] = []
    source = manifest["source"]
    for key in ("repository", "ref", "commit", "tag_object"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            source_findings.append(f"source.{key} pin is missing")

    schema_entries = 0
    for index, item in enumerate(manifest["upstream_files"]):
        if not isinstance(item, dict):
            source_findings.append(f"upstream_files[{index}] is not an object")
            continue
        relative = item.get("path")
        expected = item.get("sha256")
        if not isinstance(relative, str) or not _safe_relative_path(relative):
            source_findings.append(f"upstream_files[{index}] has an unsafe path")
            continue
        if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            source_findings.append(f"upstream_files[{index}] has an invalid SHA-256 pin")
            continue
        if item.get("source_kind") == "bundled-client-schema":
            schema_entries += 1
        file_path = path.parent.joinpath(*PurePosixPath(relative).parts)
        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(path.parent.resolve(strict=True))
            actual = _sha256(resolved)
        except (OSError, ValueError):
            actual = None
        if actual is None:
            finding = f"pinned file is missing or unreadable: {relative}"
            source_findings.append(finding)
            if item.get("source_kind") == "bundled-client-schema":
                schema_findings.append(finding)
        elif actual != expected:
            finding = f"SHA-256 mismatch: {relative}"
            source_findings.append(finding)
            if item.get("source_kind") == "bundled-client-schema":
                schema_findings.append(finding)
        if item.get("source_kind") == "bundled-client-schema" and actual == expected:
            try:
                schema_data = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                schema_findings.append("bundled client schema is not readable JSON")
            else:
                release = manifest["release"]
                if not isinstance(schema_data, dict):
                    schema_findings.append("bundled client schema must be a JSON object")
                elif schema_data.get("protocol") != release.get("protocol"):
                    schema_findings.append("manifest protocol does not match bundled schema")
                else:
                    if schema_data.get("schema_version") != 1:
                        schema_findings.append("bundled schema_version is not supported")
                    if not _contains_endpoint_generation(schema_data):
                        schema_findings.append("bundled schema lacks endpoint generation capability")
    if schema_entries != 1:
        schema_findings.append("manifest must pin exactly one bundled client schema")
    return (
        _category("drift" if source_findings else "ok", source_findings),
        _category("drift" if schema_findings else "ok", schema_findings),
    )


def _check_binary_pins(manifest: dict[str, Any], binary_path: Path | None) -> dict[str, Any]:
    release = manifest["release"]
    findings: list[str] = []
    assets = release.get("assets")
    hashes = release.get("sha256")
    if not isinstance(assets, dict) or not assets:
        findings.append("release assets pin is missing")
        assets = {}
    if not isinstance(hashes, dict) or not hashes:
        findings.append("release SHA-256 pin is missing")
        hashes = {}
    if isinstance(assets, dict):
        for platform, url in assets.items():
            if not isinstance(platform, str) or not isinstance(url, str) or not _official_asset_url(url):
                findings.append("release contains an invalid or non-official asset URL")
            digest = hashes.get(platform) if isinstance(hashes, dict) else None
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                findings.append(f"release SHA-256 pin is missing for {platform}")
    for platform in hashes if isinstance(hashes, dict) else ():
        if platform not in assets:
            findings.append(f"release hash has no matching asset for {platform}")
    if not isinstance(release.get("version"), str) or not _VERSION_RE.fullmatch(release["version"]):
        findings.append("release version pin is invalid")
    if not isinstance(release.get("protocol"), int) or release["protocol"] < 1:
        findings.append("release protocol pin is invalid")
    if not isinstance(release.get("endpoint_generation"), int) or release["endpoint_generation"] < 0:
        findings.append("release endpoint_generation pin is invalid")

    if binary_path is not None and not findings:
        platform = _platform_key()
        expected = hashes.get(platform)
        if expected is None:
            findings.append(f"no binary pin is defined for local platform {platform}")
        else:
            try:
                actual = _sha256(binary_path)
            except OSError:
                actual = None
            if actual is None:
                findings.append("requested local binary is missing or unreadable")
            elif actual != expected:
                findings.append("local binary SHA-256 does not match the pinned release")
    elif binary_path is None:
        findings.append("no local binary was supplied; only release metadata pins were checked")
    return _category("drift" if any("invalid" in item or "missing" in item or "does not match" in item for item in findings) else "unknown" if findings else "ok", findings)


def _check_server(endpoint: str | None, manifest: dict[str, Any]) -> dict[str, Any]:
    if not endpoint:
        return _category("unknown", ["no Herdr server endpoint was supplied"])
    try:
        from .transport import request

        response = request(endpoint, "ping", {})
    except Exception as exc:  # the transport error is intentionally reduced to avoid leaking paths or output
        return _category("unknown", [f"server snapshot failed: {type(exc).__name__}"])
    if not isinstance(response, dict):
        return _category("unknown", ["server snapshot returned an unexpected shape"])
    capabilities = response.get("capabilities")
    generation = _first_int(capabilities, ("endpoint_protocol_generation", "endpoint_generation"))
    protocol = _first_int(response, ("protocol", "protocol_version"))
    version = response.get("version")
    expected_generation = manifest["release"].get("endpoint_generation")
    expected_protocol = manifest["release"].get("protocol")
    findings: list[str] = []
    if generation is None:
        findings.append("server did not report endpoint protocol generation")
    elif generation < expected_generation:
        findings.append("server endpoint generation is older than the pinned client")
    if protocol is None:
        findings.append("server did not report protocol version")
    elif protocol != expected_protocol:
        findings.append("server protocol differs from the pinned client protocol")
    if isinstance(version, str):
        findings.insert(0, f"server version: {version}")
    if findings and any("did not report" in item for item in findings):
        status = "unknown" if any("did not report" in item for item in findings) else "drift"
        return _category(status, findings)
    if any("differs" in item or "older" in item for item in findings):
        return _category("drift", findings)
    return _category("ok", findings)


def _compare_latest(manifest: dict[str, Any], source: str, latest: dict[str, Any]) -> dict[str, Any]:
    tag = latest.get("version") or latest.get("tag_name")
    if not isinstance(tag, str):
        return _category("unknown", [f"{source} has no release version field"])
    current = manifest["release"].get("version")
    current_tuple = _version_tuple(current)
    latest_tuple = _version_tuple(tag)
    if current_tuple is None or latest_tuple is None:
        return _category("unknown", ["release version format is unsupported"])
    if latest_tuple > current_tuple:
        return _category("update_available", [f"{source} reports a newer release {tag}"])
    if latest_tuple < current_tuple:
        return _category("ahead_of_upstream", [f"pinned release {current} is newer than {source} release {tag}"])
    findings: list[str] = []
    latest_protocol = latest.get("protocol")
    latest_generation = latest.get("endpoint_generation", latest.get("endpoint_protocol_generation"))
    if isinstance(latest_protocol, int) and latest_protocol != manifest["release"].get("protocol"):
        findings.append("latest release protocol differs from the pinned release")
    if isinstance(latest_generation, int) and latest_generation != manifest["release"].get("endpoint_generation"):
        findings.append("latest release endpoint generation differs from the pinned release")
    if findings:
        return _category("drift", findings)
    return _category("current", [f"{source} reports the pinned release {tag}"])


def _contains_endpoint_generation(schema: Any) -> bool:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "endpoint_protocol_generation":
                return True
            if _contains_endpoint_generation(value):
                return True
    elif isinstance(schema, list):
        return any(_contains_endpoint_generation(item) for item in schema)
    return False


def _first_int(value: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    for child in value.values():
        if isinstance(child, dict):
            found = _first_int(child, keys)
            if found is not None:
                return found
    return None


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _official_asset_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {"github.com", "objects.githubusercontent.com", "herdr.dev"}


def _platform_key() -> str:
    import platform

    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"aarch64", "arm64"} else "x86_64" if machine in {"x86_64", "amd64"} else machine
    family = "macos" if system == "darwin" else system
    return f"{family}-{arch}"


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value)
    return tuple(int(part) for part in match.groups()) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _category(status: str, findings: list[str]) -> dict[str, Any]:
    return {"status": status, "findings": findings}


def _manifest_label(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return "external manifest"


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"official metadata request failed with HTTP {exc.code}"
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return "official metadata request failed or timed out"
    if isinstance(exc, OSError):
        return "official metadata request failed"
    message = str(exc)
    return message if len(message) <= 160 else message[:157] + "..."

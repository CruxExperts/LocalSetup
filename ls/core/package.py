from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Any

from .boundary import scan_tar_for_leaks
from .framework_version import framework_version
from .manifests import load_pack_config
from .paths import repo_path
from .source import source_commit, source_tag
from .versioning import read_version
from .versioning_models import SemVer
from .sdk_payload.integrity import verify as verify_sdk
from .sdk_payload.artifacts import inspect_artifact as inspect_sdk_artifact
from .sdk_payload.sbom import components as sdk_components


ARTIFACT_METADATA_PATH = "ls/artifact-metadata.json"
MAX_FRAMEWORK_VERSION_BYTES = 128


def _framework_version_for_repo(repo_root: Path) -> str:
    if (repo_root / "VERSION").is_file():
        return str(read_version(repo_root))
    return framework_version()


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_file(artifact_path: Path) -> Path:
    digest = sha256_file(artifact_path)
    sha_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
    sha_path.write_text(f"{digest}  {artifact_path.name}\n", encoding="utf-8")
    return sha_path


def _dependency_components_from_lines(lines: list[str]) -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("--") or line.startswith(("-", "git+", "http://", "https://")):
            continue
        requirement = line.rstrip("\\").strip()
        if "==" in requirement:
            name, version = requirement.split("==", 1)
            version = version.split(";", 1)[0].strip()
        else:
            name = requirement
            for marker in (">=", "<=", "~=", "!=", ">", "<", "["):
                if marker in name:
                    name = name.split(marker, 1)[0]
                    break
            version = ""
        name = name.strip()
        if name:
            component = {"type": "library", "name": name}
            if version:
                component["version"] = version
            components.append(component)
    return components


def _components_from_uv_lock(repo_root: Path) -> list[dict[str, str]]:
    lock_path = repo_root / "uv.lock"
    if not lock_path.exists():
        return []
    lock = _load_toml(lock_path)
    packages = lock.get("package", [])
    components: list[dict[str, str]] = []
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        source = package.get("source", {})
        if isinstance(source, dict) and source.get("editable") == ".":
            continue
        name = package.get("name")
        if not isinstance(name, str) or not name:
            continue
        component = {"type": "library", "name": name}
        version = package.get("version")
        if isinstance(version, str) and version:
            component["version"] = version
        components.append(component)
    return sorted(components, key=_component_key)


def _components_from_pyproject(repo_root: Path) -> list[dict[str, str]]:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        return []
    project = _load_toml(pyproject).get("project", {})
    if not isinstance(project, dict):
        return []
    dependencies = project.get("dependencies", [])
    return _dependency_components_from_lines([str(item) for item in dependencies if isinstance(item, str)])


def _components_for_sbom(repo_root: Path) -> list[dict[str, Any]]:
    result = _components_from_uv_lock(repo_root) or _components_from_pyproject(repo_root)
    sdk_root = repo_root / "vendor" / "lscli"
    if sdk_root.exists() or sdk_root.is_symlink():
        result.extend(sdk_components(verify_sdk(sdk_root)))
    return result


def _framework_version_from_artifact(artifact_path: Path) -> str:
    with tarfile.open(artifact_path, "r:*") as tar:
        members = [member for member in tar.getmembers() if member.name == "VERSION"]
        if not members:
            raise ValueError("framework VERSION not found in artifact")
        if len(members) != 1:
            raise ValueError("artifact must contain exactly one framework VERSION entry")
        member = members[0]
        if not member.isfile():
            raise ValueError("framework VERSION entry in artifact must be a regular file")
        if member.size < 0 or member.size > MAX_FRAMEWORK_VERSION_BYTES:
            raise ValueError(
                f"framework VERSION entry exceeds the {MAX_FRAMEWORK_VERSION_BYTES}-byte limit"
            )
        handle = tar.extractfile(member)
        if handle is None:
            raise ValueError("framework VERSION could not be read from artifact")
        data = handle.read(MAX_FRAMEWORK_VERSION_BYTES + 1)
        if len(data) != member.size:
            raise ValueError("framework VERSION entry is truncated in artifact")
        try:
            return str(SemVer.parse(data.decode("utf-8").strip()))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"invalid framework VERSION in artifact: {exc}") from exc


def _expected_components_from_artifact(artifact_path: Path) -> list[dict[str, str]]:
    with tarfile.open(artifact_path, "r:*") as tar:
        try:
            member = tar.getmember("uv.lock")
        except KeyError:
            return []
        handle = tar.extractfile(member)
        if handle is None:
            return []
        data = handle.read()
    lock = tomllib.loads(data.decode("utf-8"))
    packages = lock.get("package", []) if isinstance(lock, dict) else []
    components: list[dict[str, str]] = []
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        source = package.get("source", {})
        if isinstance(source, dict) and source.get("editable") == ".":
            continue
        name = package.get("name")
        if not isinstance(name, str) or not name:
            continue
        component = {"type": "library", "name": name}
        version = package.get("version")
        if isinstance(version, str) and version:
            component["version"] = version
        components.append(component)
    return sorted(components, key=_component_key)


def _component_key(component: dict[str, Any]) -> tuple[str, str]:
    return (str(component.get("name", "")).lower(), str(component.get("version", "")))


def write_cyclonedx_sbom(repo_root: Path, artifact_path: Path, metadata: dict[str, Any]) -> Path:
    sbom_path = artifact_path.with_name(f"{artifact_path.name}.cdx.json")
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": metadata["pack_id"],
                "version": _framework_version_from_artifact(artifact_path),
                "bom-ref": metadata["pack_id"],
            },
            "properties": [
                {"name": "localsetup:source_commit", "value": metadata.get("source_commit", "unknown")},
                {"name": "localsetup:artifact", "value": artifact_path.name},
            ],
        },
        "components": _components_for_sbom(repo_root),
    }
    sbom_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sbom_path


def write_source_sbom(repo_root: Path, output_path: Path) -> dict[str, Any]:
    pack = load_pack_config(repo_root)
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": pack.pack_id, "version": _framework_version_for_repo(repo_root)}},
        "components": _components_for_sbom(repo_root),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "sbom": str(output_path), "component_count": len(payload["components"])}


def write_installed_sbom(repo_root: Path, target_root: Path, output_path: Path) -> dict[str, Any]:
    from .lockfile import load_json
    from .manifests import load_pack_config
    from .paths import repo_path

    pack = load_pack_config(repo_root)
    lock = load_json(repo_path(target_root, pack.lockfile, "repo.lockfile"))
    components = [
        {"type": "file", "name": Path(path).name, "properties": [{"name": "localsetup:path", "value": path}]}
        for path in [*lock.get("installed_skills", []), *lock.get("installed_workflows", [])]
    ]
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "localsetup-installed", "version": _framework_version_for_repo(repo_root)}},
        "components": components,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "sbom": str(output_path), "component_count": len(components)}


def _artifact_metadata(repo_root: Path, output_path: Path, public_paths: list[str], pack_id: str, version: int) -> dict[str, Any]:
    tag = source_tag(repo_root)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "artifact": output_path.name,
        "pack_id": pack_id,
        "version": version,
        "source_commit": source_commit(repo_root),
        "public_paths": public_paths,
        "created_at_unix": int(time.time()),
    }
    sdk_root = repo_root / "vendor" / "lscli"
    if sdk_root.exists() or sdk_root.is_symlink():
        verify_sdk(sdk_root)
        metadata["sdk_manifest_sha256"] = sha256_file(sdk_root / "manifest.json")
    if tag:
        metadata["source_tag"] = tag
    return metadata


def build_public_artifact(repo_root: Path, output_path: Path) -> dict:
    pack = load_pack_config(repo_root)
    added: list[str] = []
    metadata = _artifact_metadata(repo_root, output_path, pack.public_paths, pack.pack_id, pack.version)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def is_package_runtime_data(path_parts: list[str]) -> bool:
        """Exclude generated package runtime state, not source assets."""
        return (
            len(path_parts) >= 5
            and path_parts[0] == "ls"
            and path_parts[1] in {"skills", "workflows"}
            and path_parts[3:5] == ["scripts", "data"]
        )

    def include_public(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = tarinfo.name.strip("/")
        parts = set(name.split("/"))
        path_parts = name.split("/")
        if (
            "__pycache__" in parts
            or ".cache" in parts
            or ".pytest_cache" in parts
            or ".mypy_cache" in parts
            or ".ruff_cache" in parts
            or name.endswith((".pyc", ".pyo"))
        ):
            return None
        if is_package_runtime_data(path_parts):
            return None
        for private in pack.private_paths:
            private_name = private.rstrip("/")
            if name == private_name or name.startswith(private_name + "/"):
                return None
        added.append(name)
        return tarinfo

    with tarfile.open(output_path, "w:gz") as tar:
        for rel in pack.public_paths:
            src = repo_path(repo_root, rel, "public path")
            if src.exists():
                tar.add(src, arcname=rel, filter=include_public)
        metadata_bytes = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        info = tarfile.TarInfo(ARTIFACT_METADATA_PATH)
        info.size = len(metadata_bytes)
        info.mtime = metadata["created_at_unix"]
        tar.addfile(info, fileobj=io.BytesIO(metadata_bytes))
        added.append(ARTIFACT_METADATA_PATH)

    leaks = scan_tar_for_leaks(output_path, pack.private_paths)
    sha_path = write_sha256_file(output_path)
    sbom_path = write_cyclonedx_sbom(repo_root, output_path, metadata)
    return {
        "artifact": str(output_path),
        "sha256": str(sha_path),
        "sbom": str(sbom_path),
        "files": added,
        "leaks": leaks,
        "manifest": metadata,
    }


def read_artifact_metadata(artifact_path: Path) -> dict[str, Any]:
    with tarfile.open(artifact_path, "r:*") as tar:
        try:
            member = tar.getmember(ARTIFACT_METADATA_PATH)
        except KeyError as exc:
            raise ValueError(f"artifact metadata not found: {ARTIFACT_METADATA_PATH}") from exc
        handle = tar.extractfile(member)
        if handle is None:
            raise ValueError(f"artifact metadata could not be read: {ARTIFACT_METADATA_PATH}")
        data = json.loads(handle.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("artifact metadata is not a JSON object")
    return data


def parse_sha256_file(path: Path) -> tuple[str, str | None]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty sha256 file: {path}")
    first = text.splitlines()[0].strip()
    parts = first.split()
    digest = parts[0]
    name = parts[1].lstrip("*") if len(parts) > 1 else None
    if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
        raise ValueError(f"invalid sha256 digest in {path}")
    return digest.lower(), name


def verify_cyclonedx_sbom(sbom_path: Path, artifact_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if not sbom_path.is_file():
        return {"name": "sbom", "ok": False, "error": f"SBOM not found: {sbom_path}"}
    try:
        payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"name": "sbom", "ok": False, "error": f"invalid SBOM JSON: {exc}"}
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict) or not isinstance(payload.get("components"), list):
        return {"name": "sbom", "ok": False, "error": "invalid SBOM object shape"}
    properties = {
        str(item.get("name")): str(item.get("value"))
        for item in payload.get("metadata", {}).get("properties", [])
        if isinstance(item, dict)
    }
    component = payload.get("metadata", {}).get("component", {})
    component = component if isinstance(component, dict) else {}
    components = payload.get("components", [])
    try:
        expected_framework_version = _framework_version_from_artifact(artifact_path)
        expected_components = _expected_components_from_artifact(artifact_path)
        sdk = inspect_sdk_artifact(artifact_path, required=False, expected_digest=metadata.get("sdk_manifest_sha256"))
        vendored = sdk_components(sdk["manifest"]) if sdk else []
        expected_components.extend(vendored)
    except (OSError, ValueError, tarfile.TarError) as exc:
        return {"name": "sbom", "ok": False, "error": str(exc)}
    actual_keys = {_component_key(item) for item in components if isinstance(item, dict)}
    expected_keys = {_component_key(item) for item in expected_components}
    missing = sorted(f"{name}=={version}" if version else name for name, version in expected_keys - actual_keys)
    unexpected = sorted(f"{name}=={version}" if version else name for name, version in actual_keys - expected_keys)
    checks = [
        payload.get("bomFormat") == "CycloneDX",
        payload.get("specVersion") == "1.6",
        isinstance(components, list),
        component.get("name") == metadata.get("pack_id"),
        component.get("version") == expected_framework_version,
        properties.get("localsetup:artifact") == artifact_path.name,
        properties.get("localsetup:source_commit") == metadata.get("source_commit"),
        not missing,
        not unexpected,
        all(sum(item == expected for item in components) == 1 for expected in vendored),
        len(actual_keys) == len(components),
    ]
    return {
        "name": "sbom",
        "ok": all(checks),
        "path": str(sbom_path),
        "bomFormat": payload.get("bomFormat"),
        "component": component.get("name"),
        "component_version": component.get("version"),
        "expected_framework_version": expected_framework_version,
        "artifact": properties.get("localsetup:artifact"),
        "source_commit": properties.get("localsetup:source_commit"),
        "component_count": len(components) if isinstance(components, list) else None,
        "expected_component_count": len(expected_components),
        "missing_components": missing,
        "unexpected_components": unexpected,
    }


def verify_release_artifact(
    artifact_path: Path,
    *,
    sha256_path: Path | None = None,
    sbom_path: Path | None = None,
    expected_commit: str | None = None,
    expected_tag: str | None = None,
) -> dict[str, Any]:
    if not artifact_path.is_file():
        raise ValueError(f"artifact not found: {artifact_path}")
    sha_path = sha256_path or artifact_path.with_name(f"{artifact_path.name}.sha256")
    checks: list[dict[str, Any]] = []
    if not sha_path.is_file():
        raise ValueError(f"sha256 file not found: {sha_path}")
    expected_digest, expected_name = parse_sha256_file(sha_path)
    actual_digest = sha256_file(artifact_path)
    checks.append(
        {
            "name": "sha256",
            "ok": expected_digest == actual_digest,
            "expected": expected_digest,
            "actual": actual_digest,
        }
    )
    if expected_name and Path(expected_name).name != artifact_path.name:
        checks.append(
            {
                "name": "sha256_filename",
                "ok": False,
                "expected": Path(expected_name).name,
                "actual": artifact_path.name,
            }
        )
    metadata = read_artifact_metadata(artifact_path)
    checks.append({"name": "artifact_metadata", "ok": True, "path": ARTIFACT_METADATA_PATH})
    if metadata.get("artifact") and metadata["artifact"] != artifact_path.name:
        checks.append({"name": "metadata_artifact", "ok": False, "expected": artifact_path.name, "actual": metadata["artifact"]})
    if expected_commit:
        checks.append(
            {
                "name": "source_commit",
                "ok": metadata.get("source_commit") == expected_commit,
                "expected": expected_commit,
                "actual": metadata.get("source_commit"),
            }
        )
    if expected_tag:
        checks.append(
            {
                "name": "source_tag",
                "ok": metadata.get("source_tag") == expected_tag,
                "expected": expected_tag,
                "actual": metadata.get("source_tag"),
            }
        )
    sbom = sbom_path or artifact_path.with_name(f"{artifact_path.name}.cdx.json")
    checks.append(verify_cyclonedx_sbom(sbom, artifact_path, metadata))
    ok = all(check.get("ok") for check in checks)
    return {
        "ok": ok,
        "artifact": str(artifact_path),
        "sha256_file": str(sha_path),
        "sbom_file": str(sbom),
        "metadata": metadata,
        "checks": checks,
    }

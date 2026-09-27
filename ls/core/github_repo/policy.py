from __future__ import annotations

import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
from typing import Any

from .model import RepositoryTarget, digest_json


POLICY_SCHEMA_VERSION = 2
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FINGERPRINT = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PolicyError(ValueError):
    pass


def normalize_hostname(value: str) -> str:
    raw = value.strip().rstrip(".")
    if not raw or "://" in raw or "/" in raw or "\\" in raw or "@" in raw:
        raise PolicyError("hostname must be a bare GitHub host name without a scheme or path")
    try:
        normalized = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise PolicyError("hostname is not a valid IDNA host name") from exc
    labels = normalized.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise PolicyError("hostname must be a valid DNS host name")
    return normalized


def parse_target(hostname: str, repository: str) -> RepositoryTarget:
    host = normalize_hostname(hostname)
    pieces = repository.split("/")
    if len(pieces) != 2:
        raise PolicyError("repository must use OWNER/REPO format")
    owner, name = pieces
    if not _OWNER.fullmatch(owner) or not _REPOSITORY.fullmatch(name) or name in {".", ".."}:
        raise PolicyError("repository owner or name contains unsupported characters")
    return RepositoryTarget(hostname=host, owner=owner, repository=name)


def _keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise PolicyError(f"unsupported {where} field(s): {', '.join(extra)}")


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PolicyError(f"{where} must be a JSON object")
    return value


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{where} must be a boolean")
    return value


def _string(value: Any, where: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise PolicyError(f"{where} must be a string of at most {maximum} characters")
    return value


def _string_list(value: Any, where: str, *, maximum_items: int = 100, maximum_length: int = 200) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise PolicyError(f"{where} must be an array of at most {maximum_items} strings")
    result = [_string(item, where, maximum=maximum_length) for item in value]
    if len(set(result)) != len(result):
        raise PolicyError(f"{where} must not contain duplicates")
    return sorted(result)


def _tag_name(value: Any, where: str) -> str:
    tag = _string(value, where, maximum=256)
    forbidden = set(" ~^:?*[\\")
    if (
        not tag or tag.startswith(("-", "/")) or tag.endswith(("/", "."))
        or "//" in tag or ".." in tag or "@{" in tag
        or any(char in forbidden or ord(char) < 32 or ord(char) == 127 for char in tag)
        or any(part.startswith(".") or part.endswith(".lock") for part in tag.split("/"))
    ):
        raise PolicyError(f"{where} must be a valid tag name")
    return tag


def _relative_artifact_path(value: Any, where: str) -> str:
    raw = _string(value, where, maximum=1024)
    path = PurePosixPath(raw)
    if (
        not raw or "\\" in raw or ":" in raw or "\x00" in raw
        or path.is_absolute() or str(path) != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PolicyError(f"{where} must be a safe checkout-relative path")
    return raw


def _normalize_verification(value: Any) -> dict[str, Any]:
    verification = _object(value, "verification")
    _keys(verification, {"signatures", "release"}, "verification")
    result: dict[str, Any] = {}
    if "signatures" in verification:
        signatures = _object(verification["signatures"], "verification.signatures")
        _keys(signatures, {"commit_oid", "tag_name", "expected_primary_fingerprints"}, "verification.signatures")
        if set(signatures) != {"commit_oid", "tag_name", "expected_primary_fingerprints"}:
            raise PolicyError("verification.signatures requires commit_oid, tag_name, and expected_primary_fingerprints")
        commit_oid = _string(signatures["commit_oid"], "verification.signatures.commit_oid", maximum=64)
        if not _OBJECT_ID.fullmatch(commit_oid):
            raise PolicyError("verification.signatures.commit_oid must be a full lowercase Git object ID")
        fingerprints = signatures["expected_primary_fingerprints"]
        if not isinstance(fingerprints, list) or not fingerprints or len(fingerprints) > 32:
            raise PolicyError("verification.signatures.expected_primary_fingerprints must contain 1 to 32 fingerprints")
        normalized_fingerprints = []
        for fingerprint in fingerprints:
            if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint.upper()):
                raise PolicyError("verification.signatures.expected_primary_fingerprints contains an invalid fingerprint")
            normalized_fingerprints.append(fingerprint.upper())
        if len(set(normalized_fingerprints)) != len(normalized_fingerprints):
            raise PolicyError("verification.signatures.expected_primary_fingerprints must not contain duplicates")
        result["signatures"] = {
            "commit_oid": commit_oid,
            "tag_name": _tag_name(signatures["tag_name"], "verification.signatures.tag_name"),
            "expected_primary_fingerprints": sorted(normalized_fingerprints),
        }
    if "release" in verification:
        release = _object(verification["release"], "verification.release")
        _keys(release, {"release_id", "tag_name", "source_ref", "source_commit", "signer_workflow", "predicate_type", "artifacts"}, "verification.release")
        required = {"release_id", "tag_name", "source_ref", "source_commit", "signer_workflow", "predicate_type", "artifacts"}
        if set(release) != required:
            raise PolicyError("verification.release requires release, source, workflow, predicate, and artifact identities")
        release_id = release["release_id"]
        if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
            raise PolicyError("verification.release.release_id must be a positive integer")
        tag_name = _tag_name(release["tag_name"], "verification.release.tag_name")
        source_ref = _string(release["source_ref"], "verification.release.source_ref", maximum=512)
        if source_ref != f"refs/tags/{tag_name}":
            raise PolicyError("verification.release.source_ref must identify its exact tag_name")
        source_commit = _string(release["source_commit"], "verification.release.source_commit", maximum=64)
        if not _OBJECT_ID.fullmatch(source_commit):
            raise PolicyError("verification.release.source_commit must be a full lowercase Git object ID")
        signatures = result.get("signatures")
        if signatures is not None and (signatures["commit_oid"] != source_commit or signatures["tag_name"] != tag_name):
            raise PolicyError("verification.release source must match the selected signed commit and tag")
        signer_workflow = _string(release["signer_workflow"], "verification.release.signer_workflow", maximum=512)
        predicate_type = _string(release["predicate_type"], "verification.release.predicate_type", maximum=512)
        if not signer_workflow or "@" not in signer_workflow or any(ord(char) < 33 or ord(char) == 127 for char in signer_workflow):
            raise PolicyError("verification.release.signer_workflow must be a nonempty exact workflow identity")
        if not predicate_type or any(ord(char) < 33 or ord(char) == 127 for char in predicate_type):
            raise PolicyError("verification.release.predicate_type must be a nonempty exact identity")
        artifacts = release["artifacts"]
        if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 100:
            raise PolicyError("verification.release.artifacts must contain 1 to 100 assets")
        normalized_artifacts: list[dict[str, Any]] = []
        asset_ids: set[int] = set()
        names: set[str] = set()
        paths: set[str] = set()
        for index, raw_artifact in enumerate(artifacts):
            artifact = _object(raw_artifact, f"verification.release.artifacts[{index}]")
            _keys(artifact, {"asset_id", "name", "path", "expected_sha256"}, f"verification.release.artifacts[{index}]")
            if set(artifact) != {"asset_id", "name", "path", "expected_sha256"}:
                raise PolicyError(f"verification.release.artifacts[{index}] requires asset_id, name, path, and expected_sha256")
            asset_id = artifact["asset_id"]
            if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
                raise PolicyError(f"verification.release.artifacts[{index}].asset_id must be a positive integer")
            name = _string(artifact["name"], f"verification.release.artifacts[{index}].name", maximum=255)
            if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise PolicyError(f"verification.release.artifacts[{index}].name must be a nonempty filename")
            path = _relative_artifact_path(artifact["path"], f"verification.release.artifacts[{index}].path")
            digest = _string(artifact["expected_sha256"], f"verification.release.artifacts[{index}].expected_sha256", maximum=64)
            if not _SHA256.fullmatch(digest):
                raise PolicyError(f"verification.release.artifacts[{index}].expected_sha256 must be lowercase SHA-256")
            if asset_id in asset_ids or name in names or path in paths:
                raise PolicyError("verification.release artifact IDs, names, and paths must be unique")
            asset_ids.add(asset_id)
            names.add(name)
            paths.add(path)
            normalized_artifacts.append({"asset_id": asset_id, "name": name, "path": path, "expected_sha256": digest})
        result["release"] = {
            "release_id": release_id,
            "tag_name": tag_name,
            "source_ref": source_ref,
            "source_commit": source_commit,
            "signer_workflow": signer_workflow,
            "predicate_type": predicate_type,
            "artifacts": sorted(normalized_artifacts, key=lambda item: (item["asset_id"], item["name"])),
        }
    return result


def normalize_policy(raw: Any) -> dict[str, Any]:
    value = _object(raw, "policy")
    _keys(value, {"schema_version", "repository", "rulesets", "actions", "pages", "security", "releases", "repository_content", "verification"}, "policy")
    if not isinstance(value.get("schema_version"), int) or isinstance(value.get("schema_version"), bool) or value.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise PolicyError(f"policy schema_version must be {POLICY_SCHEMA_VERSION}")
    normalized: dict[str, Any] = {"schema_version": POLICY_SCHEMA_VERSION}

    if "repository" in value:
        repository = _object(value["repository"], "repository")
        _keys(repository, {"description", "homepage", "topics", "features", "merge", "visibility", "default_branch", "web_commit_signoff_required"}, "repository")
        result: dict[str, Any] = {}
        if "visibility" in repository:
            if not isinstance(repository["visibility"], str) or repository["visibility"] not in {"public", "private"}:
                raise PolicyError("repository.visibility must be public or private; internal is not supported")
            result["visibility"] = repository["visibility"]
        if "default_branch" in repository:
            branch = _string(repository["default_branch"], "repository.default_branch", maximum=255)
            forbidden = set(" ~^:?*[\\\\")
            if (
                not branch
                or branch.startswith("/")
                or branch.endswith(("/", "."))
                or "//" in branch
                or ".." in branch
                or "@{" in branch
                or any(ord(char) < 32 or ord(char) == 127 or char in forbidden for char in branch)
                or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
            ):
                raise PolicyError("repository.default_branch must be a supported Git branch name")
            result["default_branch"] = branch
        if "web_commit_signoff_required" in repository:
            result["web_commit_signoff_required"] = _boolean(
                repository["web_commit_signoff_required"],
                "repository.web_commit_signoff_required",
            )
        for key in ("description", "homepage"):
            if key in repository:
                result[key] = _string(repository[key], f"repository.{key}", maximum=350)
        if "topics" in repository:
            topics = _string_list(repository["topics"], "repository.topics", maximum_items=20, maximum_length=50)
            if any(not _TOPIC.fullmatch(item) for item in topics):
                raise PolicyError("repository.topics must use lowercase letters, digits, and hyphens")
            result["topics"] = topics
        if "features" in repository:
            features = _object(repository["features"], "repository.features")
            _keys(features, {"issues", "projects", "wiki", "downloads"}, "repository.features")
            result["features"] = {key: _boolean(features[key], f"repository.features.{key}") for key in sorted(features)}
        if "merge" in repository:
            merge = _object(repository["merge"], "repository.merge")
            merge_keys = {"allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "allow_auto_merge", "delete_branch_on_merge", "squash_merge_commit_title", "squash_merge_commit_message"}
            _keys(merge, merge_keys, "repository.merge")
            merge_result: dict[str, Any] = {}
            for key in sorted(merge):
                if key in {"squash_merge_commit_title", "squash_merge_commit_message"}:
                    allowed = {"PR_TITLE", "COMMIT_OR_PR_TITLE"} if key.endswith("title") else {"PR_BODY", "COMMIT_MESSAGES", "BLANK"}
                    if merge[key] not in allowed:
                        raise PolicyError(f"repository.merge.{key} has an unsupported value")
                    merge_result[key] = merge[key]
                else:
                    merge_result[key] = _boolean(merge[key], f"repository.merge.{key}")
            result["merge"] = merge_result
        normalized["repository"] = result

    if "rulesets" in value:
        if not isinstance(value["rulesets"], list) or len(value["rulesets"]) > 100:
            raise PolicyError("rulesets must be an array of at most 100 rulesets")
        rulesets: list[dict[str, Any]] = []
        keys = {"name", "target", "enforcement", "include", "exclude", "require_linear_history", "require_signed_commits", "block_deletions", "block_force_pushes", "required_approving_review_count", "dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval", "required_review_thread_resolution", "required_status_checks", "strict_required_status_checks"}
        for index, raw_ruleset in enumerate(value["rulesets"]):
            item = _object(raw_ruleset, f"rulesets[{index}]")
            _keys(item, keys, f"rulesets[{index}]")
            if not {"name", "target", "include"} <= set(item):
                raise PolicyError(f"rulesets[{index}] requires name, target, and include")
            target = item["target"]
            if not isinstance(target, str) or target not in {"branch", "tag"}:
                raise PolicyError(f"rulesets[{index}].target must be branch or tag")
            enforcement = item.get("enforcement", "active")
            if not isinstance(enforcement, str) or enforcement not in {"active", "disabled", "evaluate"}:
                raise PolicyError(f"rulesets[{index}].enforcement is unsupported")
            result = {
                "name": _string(item["name"], f"rulesets[{index}].name", maximum=100),
                "target": target,
                "enforcement": enforcement,
                "include": _string_list(item["include"], f"rulesets[{index}].include", maximum_items=100, maximum_length=255),
                "exclude": _string_list(item.get("exclude", []), f"rulesets[{index}].exclude", maximum_items=100, maximum_length=255),
            }
            for key in ("require_linear_history", "require_signed_commits", "block_deletions", "block_force_pushes", "dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval", "required_review_thread_resolution", "strict_required_status_checks"):
                if key in item:
                    result[key] = _boolean(item[key], f"rulesets[{index}].{key}")
            if "required_approving_review_count" in item:
                count = item["required_approving_review_count"]
                if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 6:
                    raise PolicyError(f"rulesets[{index}].required_approving_review_count must be an integer from 0 to 6")
                result["required_approving_review_count"] = count
            if "required_status_checks" in item:
                checks = item["required_status_checks"]
                if not isinstance(checks, list) or len(checks) > 100:
                    raise PolicyError(f"rulesets[{index}].required_status_checks must be an array of at most 100 items")
                normalized_checks = []
                for check_index, raw_check in enumerate(checks):
                    check = _object(raw_check, f"rulesets[{index}].required_status_checks[{check_index}]")
                    _keys(check, {"context", "integration_id"}, "required status check")
                    if "context" not in check:
                        raise PolicyError("required status check requires context")
                    normalized_check = {"context": _string(check["context"], "required status check context", maximum=255)}
                    if "integration_id" in check:
                        integration_id = check["integration_id"]
                        if not isinstance(integration_id, int) or isinstance(integration_id, bool) or integration_id <= 0:
                            raise PolicyError("required status check integration_id must be a positive integer")
                        normalized_check["integration_id"] = integration_id
                    normalized_checks.append(normalized_check)
                result["required_status_checks"] = sorted(normalized_checks, key=lambda row: (row["context"], row.get("integration_id", 0)))
            if "strict_required_status_checks" in item:
                result["strict_required_status_checks"] = _boolean(item["strict_required_status_checks"], f"rulesets[{index}].strict_required_status_checks")
            if result.get("required_status_checks") and "strict_required_status_checks" not in result:
                raise PolicyError(f"rulesets[{index}] with required_status_checks must specify strict_required_status_checks")
            if "strict_required_status_checks" in result and not result.get("required_status_checks"):
                raise PolicyError(f"rulesets[{index}].strict_required_status_checks requires a non-empty required_status_checks array")
            rulesets.append(result)
        names = [(item["target"], item["name"].casefold()) for item in rulesets]
        if len(set(names)) != len(names):
            raise PolicyError("ruleset target/name pairs must be unique")
        normalized["rulesets"] = sorted(rulesets, key=lambda item: (item["target"], item["name"].casefold()))

    if "actions" in value:
        actions = _object(value["actions"], "actions")
        action_keys = {"enabled", "allowed_actions", "selected_actions", "sha_pinning_required", "default_workflow_permissions", "can_approve_pull_request_reviews"}
        _keys(actions, action_keys, "actions")
        result = {}
        for key in ("enabled", "sha_pinning_required", "can_approve_pull_request_reviews"):
            if key in actions:
                result[key] = _boolean(actions[key], f"actions.{key}")
        if "allowed_actions" in actions:
            if not isinstance(actions["allowed_actions"], str) or actions["allowed_actions"] not in {"all", "local_only", "selected"}:
                raise PolicyError("actions.allowed_actions is unsupported")
            result["allowed_actions"] = actions["allowed_actions"]
        if "selected_actions" in actions:
            selected = _object(actions["selected_actions"], "actions.selected_actions")
            _keys(selected, {"github_owned_allowed", "verified_allowed", "patterns_allowed"}, "actions.selected_actions")
            selected_result = {}
            for key in ("github_owned_allowed", "verified_allowed"):
                if key in selected:
                    selected_result[key] = _boolean(selected[key], f"actions.selected_actions.{key}")
            if "patterns_allowed" in selected:
                selected_result["patterns_allowed"] = _string_list(selected["patterns_allowed"], "actions.selected_actions.patterns_allowed", maximum_items=100, maximum_length=255)
            result["selected_actions"] = selected_result
        if result.get("allowed_actions") == "selected" and "selected_actions" not in result:
            raise PolicyError("actions.selected_actions is required when allowed_actions is selected")
        if "default_workflow_permissions" in actions:
            if not isinstance(actions["default_workflow_permissions"], str) or actions["default_workflow_permissions"] not in {"read", "write"}:
                raise PolicyError("actions.default_workflow_permissions must be read or write")
            result["default_workflow_permissions"] = actions["default_workflow_permissions"]
        normalized["actions"] = result

    if "pages" in value:
        pages = _object(value["pages"], "pages")
        _keys(pages, {"enabled", "build_type", "source", "cname", "https_enforced"}, "pages")
        result = {}
        if "enabled" in pages:
            result["enabled"] = _boolean(pages["enabled"], "pages.enabled")
        if "build_type" in pages:
            if not isinstance(pages["build_type"], str) or pages["build_type"] not in {"legacy", "workflow"}:
                raise PolicyError("pages.build_type must be legacy or workflow")
            result["build_type"] = pages["build_type"]
        if "source" in pages:
            source = _object(pages["source"], "pages.source")
            _keys(source, {"branch", "path"}, "pages.source")
            if set(source) != {"branch", "path"}:
                raise PolicyError("pages.source requires branch and path")
            path = source["path"]
            if not isinstance(path, str) or path not in {"/", "/docs"}:
                raise PolicyError("pages.source.path must be / or /docs")
            result["source"] = {"branch": _string(source["branch"], "pages.source.branch", maximum=255), "path": path}
        if "cname" in pages:
            result["cname"] = None if pages["cname"] is None else _string(pages["cname"], "pages.cname", maximum=253)
        if "https_enforced" in pages:
            result["https_enforced"] = _boolean(pages["https_enforced"], "pages.https_enforced")
        if result.get("enabled") is True and ("build_type" not in result or "source" not in result):
            raise PolicyError("pages.enabled=true requires build_type and source")
        normalized["pages"] = result

    if "security" in value:
        security = _object(value["security"], "security")
        _keys(security, {"dependabot_alerts", "automated_security_fixes", "private_vulnerability_reporting", "advanced_security", "code_security", "secret_scanning", "secret_scanning_push_protection", "secret_scanning_non_provider_patterns", "secret_scanning_ai_detection"}, "security")
        normalized["security"] = {key: _boolean(security[key], f"security.{key}") for key in sorted(security)}

    if "releases" in value:
        releases = _object(value["releases"], "releases")
        _keys(releases, {"immutable"}, "releases")
        normalized["releases"] = {"immutable": _boolean(releases["immutable"], "releases.immutable")} if "immutable" in releases else {}

    if "repository_content" in value:
        content = _object(value["repository_content"], "repository_content")
        _keys(content, {"social_preview"}, "repository_content")
        result = {}
        if "social_preview" in content:
            preview = _object(content["social_preview"], "repository_content.social_preview")
            _keys(preview, {"action", "asset_path"}, "repository_content.social_preview")
            action = preview.get("action")
            if not isinstance(action, str) or action not in {"present", "absent"}:
                raise PolicyError("repository_content.social_preview.action must be present or absent")
            preview_result: dict[str, str] = {"action": action}
            if action == "present":
                asset_path = _string(preview.get("asset_path"), "repository_content.social_preview.asset_path", maximum=255)
                path = PurePosixPath(asset_path)
                if (
                    not asset_path
                    or "\x00" in asset_path
                    or "\\" in asset_path
                    or ":" in asset_path
                    or path.is_absolute()
                    or str(path) != asset_path
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif"}
                ):
                    raise PolicyError("social preview asset_path must be a safe checkout-relative PNG, JPEG, or GIF path")
                preview_result["asset_path"] = asset_path
            elif "asset_path" in preview:
                raise PolicyError("social preview asset_path is allowed only for action present")
            result["social_preview"] = preview_result
        normalized["repository_content"] = result

    if "verification" in value:
        normalized["verification"] = _normalize_verification(value["verification"])

    raw_repository = value.get("repository")
    if isinstance(raw_repository, dict) and "visibility" in raw_repository:
        if set(value) != {"schema_version", "repository"} or set(raw_repository) != {"visibility"}:
            raise PolicyError("repository.visibility must be the only setting in its plan")
    if isinstance(raw_repository, dict) and "default_branch" in raw_repository:
        if set(value) != {"schema_version", "repository"} or set(raw_repository) != {"default_branch"}:
            raise PolicyError("repository.default_branch must be the only setting in its plan")
    return normalized


def read_policy(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(path.expanduser(), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PolicyError("policy JSON must be a regular file")
        maximum = 1024 * 1024
        if info.st_size > maximum:
            raise PolicyError("policy JSON exceeds the supported size limit")
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, min(32 * 1024, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > maximum:
                raise PolicyError("policy JSON exceeds the supported size limit")
        raw = json.loads(bytes(chunks).decode("utf-8"), object_pairs_hook=_object_no_duplicate_keys)
    except PolicyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PolicyError(f"could not read policy JSON from {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return normalize_policy(raw)


def _object_no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def policy_digest(policy: dict[str, Any]) -> str:
    return digest_json(policy)


def policy_text(policy: dict[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

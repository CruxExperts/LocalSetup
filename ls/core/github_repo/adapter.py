from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from typing import Any, Protocol
from urllib.parse import quote

from .model import RepositoryTarget, canonical_ruleset
from .interfaces import describe_operation_interface


GITHUB_API_VERSION = "2026-03-10"
_HTTP_STATUS = re.compile(r"HTTP(?:/\d(?:\.\d)?)?\s+(\d{3})", re.IGNORECASE)
_TOKEN_SCOPES = re.compile(r"Token scopes:\s*([^\r\n]*)", re.IGNORECASE)
_PAGE_SIZE = 100
_PAGE_LIMIT = 100
_RELEASE_ASSET_RELEASE_LIMIT = 100
_RELEASE_ASSET_PAGE_LIMIT = 10
_MAX_GH_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_GH_INPUT_BYTES = 2 * 1024 * 1024
_RELEASE_VERIFY_ASSET_HELP = re.compile(rb"(?im)^\s*(?:usage:\s*)?gh(?:\.exe)?\s+release\s+verify-asset\b")
_ATTESTATION_VERIFY_HELP = re.compile(rb"(?im)^\s*(?:usage:\s*)?gh(?:\.exe)?\s+attestation\s+verify\b")


class GitHubError(RuntimeError):
    def __init__(self, status: int | None, *, operation: str, ambiguous: bool = False):
        self.status = status
        self.operation = operation
        self.ambiguous = ambiguous
        if status in {401, 403}:
            code = "authentication_or_permission_denied"
        elif status == 404:
            code = "resource_unavailable_or_not_found"
        elif status in {409, 422}:
            code = "request_conflict_or_rejected"
        elif ambiguous:
            code = "remote_outcome_unknown"
        else:
            code = "github_request_failed"
        self.code = code
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{code}: {operation}{suffix}")


class GitHubAdapter(Protocol):
    def auth_capabilities(self) -> dict[str, Any]: ...
    def actor(self) -> dict[str, Any]: ...
    def repository(self) -> dict[str, Any]: ...
    def topics(self) -> list[str]: ...
    def rulesets(self) -> list[dict[str, Any]]: ...
    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]: ...
    def actions_permissions(self) -> dict[str, Any]: ...
    def workflow_permissions(self) -> dict[str, Any]: ...
    def selected_actions(self) -> dict[str, Any]: ...
    def pages(self) -> dict[str, Any]: ...
    def vulnerability_alerts_enabled(self) -> bool: ...
    def automated_security_fixes_enabled(self) -> bool: ...
    def private_vulnerability_reporting_enabled(self) -> bool: ...
    def dependabot_alert_count(self) -> int: ...
    def code_scanning_alert_count(self) -> int: ...
    def immutable_releases_enabled(self) -> bool: ...
    def releases_summary(self) -> dict[str, Any]: ...
    def social_preview_custom(self) -> bool: ...
    def effective_branch_rules(self, branch: str) -> list[dict[str, Any]]: ...
    def legacy_branch_protection(self, branch: str) -> dict[str, Any]: ...
    def required_signatures(self, branch: str) -> dict[str, Any]: ...
    def branch_head_sha(self, branch: str) -> str: ...
    def workflow_inventory(self) -> dict[str, Any]: ...
    def workflow_runs_summary(self, branch: str) -> dict[str, Any]: ...
    def check_runs_summary(self, ref: str) -> dict[str, Any]: ...
    def commit_status_summary(self, ref: str) -> dict[str, Any]: ...
    def environments_summary(self) -> dict[str, Any]: ...
    def deployments_summary(self) -> dict[str, Any]: ...
    def pages_read(self) -> dict[str, Any]: ...
    def pages_health(self) -> dict[str, Any]: ...
    def custom_secret_pattern_count(self) -> dict[str, Any]: ...
    def secret_scanning_alert_count(self) -> int: ...
    def sbom_summary(self) -> dict[str, Any]: ...
    def latest_release_summary(self) -> dict[str, Any]: ...
    def release_inventory(self) -> dict[str, Any]: ...
    def release_assets_summary(self) -> dict[str, Any]: ...
    def release_identity(self, release_id: int) -> dict[str, Any]: ...
    def release_asset_identity(self, release_id: int, asset_id: int) -> dict[str, Any]: ...
    def tag_commit_sha(self, tag_name: str) -> str: ...
    def verify_release_asset(self, tag_name: str, file_path: str) -> dict[str, Any]: ...
    def verify_attestation(self, file_path: str, *, signer_workflow: str, source_ref: str, predicate_type: str) -> dict[str, Any]: ...
    def operation_state(self, operation: dict[str, Any]) -> Any: ...
    def apply_operation(self, operation: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class RepoParts:
    owner: str
    name: str


def _status_from_output(text: str) -> int | None:
    match = _HTTP_STATUS.search(text)
    return int(match.group(1)) if match else None


def _clean_ruleset(value: dict[str, Any]) -> dict[str, Any]:
    return canonical_ruleset(value)


_RULESET_DETAIL_FIELDS = {"id", "name", "target", "enforcement", "conditions", "rules", "bypass_actors"}


def _checked_ruleset_detail(value: Any, ruleset_id: int, *, operation: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), int)
        or isinstance(value.get("id"), bool)
        or value.get("id") <= 0
        or value.get("id") != ruleset_id
        or not _RULESET_DETAIL_FIELDS <= set(value)
        or not isinstance(value.get("conditions"), dict)
        or not isinstance(value.get("rules"), list)
        or not isinstance(value.get("bypass_actors"), list)
    ):
        raise GitHubError(None, operation=operation)
    return _clean_ruleset(value)


def _required_state_fields(value: Any, fields: set[str], *, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not fields <= set(value):
        raise GitHubError(None, operation=operation)
    return {key: value[key] for key in fields}


class _GhProcessFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError:
        pass


def _run_bounded_gh(
    argv: list[str],
    *,
    input_data: bytes | None = None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[bytes, bytes, int]:
    """Stream gh output with a combined byte cap before any response parsing."""
    if input_data is not None and len(input_data) > _MAX_GH_INPUT_BYTES:
        raise _GhProcessFailure("request_input_limit_exceeded")
    try:
        process_env = os.environ.copy()
        if env is not None:
            process_env.update(env)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=(os.name == "posix"),
            env=process_env,
        )
    except FileNotFoundError as exc:
        raise _GhProcessFailure("gh_unavailable") from exc
    except OSError as exc:
        raise _GhProcessFailure("gh_unavailable") from exc

    assert process.stdout is not None and process.stderr is not None
    lock = threading.Lock()
    killed = threading.Event()
    too_large = threading.Event()
    captured = [0]
    outputs = {"stdout": bytearray(), "stderr": bytearray()}

    def terminate() -> None:
        if not killed.is_set():
            killed.set()
            _kill_process(process)

    def drain(stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                with lock:
                    remaining = _MAX_GH_OUTPUT_BYTES - captured[0]
                    if len(chunk) > remaining:
                        if remaining > 0:
                            outputs[name].extend(chunk[:remaining])
                            captured[0] += remaining
                        too_large.set()
                    else:
                        outputs[name].extend(chunk)
                        captured[0] += len(chunk)
                if too_large.is_set():
                    terminate()
        except OSError:
            terminate()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in readers:
        thread.start()

    writer: threading.Thread | None = None
    if input_data is not None:
        assert process.stdin is not None

        def write_input() -> None:
            try:
                process.stdin.write(input_data)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            returncode = -1
    if writer is not None:
        writer.join(timeout=1)
    for thread in readers:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in readers):
        terminate()
        raise _GhProcessFailure("gh_output_read_incomplete")
    if too_large.is_set():
        raise _GhProcessFailure("gh_output_limit_exceeded")
    if timed_out:
        raise _GhProcessFailure("gh_timeout")
    return bytes(outputs["stdout"]), bytes(outputs["stderr"]), returncode


class GhCliAdapter:
    """GitHub REST/GraphQL adapter implemented only with fixed gh API shapes."""

    def __init__(self, target: RepositoryTarget, *, gh_executable: str | None = None, timeout: float = 45.0):
        self.target = target
        self._owner = target.owner
        self._repo = target.repository
        self._gh = gh_executable or shutil.which("gh") or "gh"
        self._timeout = timeout
        self._ruleset_create_ids: dict[tuple[str, str], int] = {}
        self._ruleset_create_attempted: set[tuple[str, str]] = set()
        self._verification_commands_available: bool | None = None

    @staticmethod
    def _ruleset_key(desired: Any) -> tuple[str, str]:
        if not isinstance(desired, dict):
            raise GitHubError(None, operation="repository_ruleset")
        name, target = desired.get("name"), desired.get("target")
        if not isinstance(name, str) or target not in {"branch", "tag"}:
            raise GitHubError(None, operation="repository_ruleset")
        return name, target

    def _repo_path(self, suffix: str = "") -> str:
        base = f"repos/{self._owner}/{self._repo}"
        return f"{base}/{suffix}" if suffix else base

    def _request(self, method: str, endpoint: str, *, body: dict[str, Any] | None = None, operation: str) -> Any:
        argv = [
            self._gh,
            "api",
            "--hostname",
            self.target.hostname,
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        ]
        if body is not None:
            argv.extend(["--input", "-"])
        argv.append(endpoint)
        try:
            input_data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            stdout, stderr, returncode = _run_bounded_gh(argv, input_data=input_data, timeout=self._timeout)
        except _GhProcessFailure as exc:
            # Never include gh output or exception text: those may contain private response details.
            raise GitHubError(None, operation=operation, ambiguous=method != "GET") from exc
        if returncode:
            status = _status_from_output((stderr + b"\n" + stdout).decode("utf-8", errors="replace"))
            raise GitHubError(status, operation=operation, ambiguous=method != "GET" and status is None)
        output = stdout.decode("utf-8", errors="strict").strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError(None, operation=operation, ambiguous=method != "GET") from exc

    def _paginate_result(
        self,
        suffix: str,
        *,
        operation: str,
        collection_key: str | None = None,
        page_size: int = _PAGE_SIZE,
        page_limit: int = _PAGE_LIMIT,
    ) -> dict[str, Any]:
        result: list[dict[str, Any]] = []
        reported_total: int | None = None
        for page in range(1, page_limit + 1):
            separator = "&" if "?" in suffix else "?"
            endpoint = f"{self._repo_path(suffix)}{separator}per_page={page_size}&page={page}"
            rows = self._request("GET", endpoint, operation=operation)
            if collection_key is None:
                page_rows = rows
            elif isinstance(rows, dict):
                page_rows = rows.get(collection_key)
                total = rows.get("total_count")
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                    reported_total = total
            else:
                page_rows = None
            if not isinstance(page_rows, list):
                raise GitHubError(None, operation=operation)
            if any(not isinstance(row, dict) for row in page_rows):
                raise GitHubError(None, operation=operation)
            result.extend(page_rows)
            if reported_total is not None:
                if len(result) >= reported_total:
                    return {"items": result, "pages": page, "complete": True, "reported_total": reported_total}
                if len(page_rows) < page_size:
                    return {"items": result, "pages": page, "complete": False, "reported_total": reported_total}
            elif len(page_rows) < page_size:
                return {"items": result, "pages": page, "complete": True, "reported_total": None}
        return {"items": result, "pages": page_limit, "complete": False, "reported_total": reported_total}

    def _paginate(self, suffix: str, *, operation: str, collection_key: str | None = None) -> list[dict[str, Any]]:
        result = self._paginate_result(suffix, operation=operation, collection_key=collection_key)
        if not result["complete"]:
            raise GitHubError(None, operation=f"{operation}_pagination_limit")
        return result["items"]

    def auth_capabilities(self) -> dict[str, Any]:
        argv = [self._gh, "auth", "status", "--hostname", self.target.hostname]
        try:
            stdout, stderr, returncode = _run_bounded_gh(argv, timeout=15)
        except _GhProcessFailure:
            return {"authenticated": False, "scope_visibility": "unknown", "scopes": None}
        if returncode:
            return {"authenticated": False, "scope_visibility": "unknown", "scopes": None}
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        match = _TOKEN_SCOPES.search(output)
        if match is None:
            return {"authenticated": True, "scope_visibility": "not_reported", "scopes": None}
        scopes = sorted(set(re.findall(r"[A-Za-z0-9:_-]+", match.group(1))))
        return {"authenticated": True, "scope_visibility": "reported", "scopes": scopes}

    def actor(self) -> dict[str, Any]:
        actor = self._request("GET", "user", operation="authenticated_actor")
        if not isinstance(actor, dict) or not isinstance(actor.get("id"), int):
            raise GitHubError(None, operation="authenticated_actor")
        return {"id": actor["id"], "login": actor.get("login")}

    def repository(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path(), operation="repository_metadata")
        if not isinstance(value, dict) or not isinstance(value.get("id"), int):
            raise GitHubError(None, operation="repository_metadata")
        return value

    def topics(self) -> list[str]:
        value = self._request("GET", self._repo_path("topics"), operation="repository_topics")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("names"), list)
            or any(not isinstance(item, str) for item in value["names"])
        ):
            raise GitHubError(None, operation="repository_topics")
        return sorted(value["names"])

    def rulesets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 1001):
            endpoint = f"{self._repo_path('rulesets')}?per_page=100&page={page}&includes_parents=true"
            rows = self._request("GET", endpoint, operation="repository_rulesets")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise GitHubError(None, operation="repository_rulesets")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise GitHubError(None, operation="repository_rulesets_pagination_limit")

    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]:
        if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
            raise GitHubError(None, operation="repository_ruleset")
        value = self._request("GET", self._repo_path(f"rulesets/{ruleset_id}"), operation="repository_ruleset")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="repository_ruleset")
        return value

    def effective_branch_rules(self, branch: str) -> list[dict[str, Any]]:
        if not isinstance(branch, str) or not branch:
            raise GitHubError(None, operation="effective_branch_rules")
        endpoint = self._repo_path(f"rules/branches/{quote(branch, safe='')}")
        value = self._request("GET", endpoint, operation="effective_branch_rules")
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise GitHubError(None, operation="effective_branch_rules")
        return value

    def legacy_branch_protection(self, branch: str) -> dict[str, Any]:
        if not isinstance(branch, str) or not branch:
            raise GitHubError(None, operation="legacy_branch_protection")
        endpoint = self._repo_path(f"branches/{quote(branch, safe='')}/protection")
        value = self._request("GET", endpoint, operation="legacy_branch_protection")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="legacy_branch_protection")
        return value

    def required_signatures(self, branch: str) -> dict[str, Any]:
        if not isinstance(branch, str) or not branch:
            raise GitHubError(None, operation="required_signatures")
        endpoint = self._repo_path(f"branches/{quote(branch, safe='')}/protection/required_signatures")
        value = self._request("GET", endpoint, operation="required_signatures")
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise GitHubError(None, operation="required_signatures")
        return {"enabled": value["enabled"]}

    def branch_head_sha(self, branch: str) -> str:
        if not isinstance(branch, str) or not branch:
            raise GitHubError(None, operation="branch_head")
        value = self._request("GET", self._repo_path(f"commits/{quote(branch, safe='')}"), operation="branch_head")
        commit = value.get("sha") if isinstance(value, dict) else None
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise GitHubError(None, operation="branch_head")
        return commit

    def actions_permissions(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("actions/permissions"), operation="actions_permissions")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="actions_permissions")
        return {key: value[key] for key in ("enabled", "allowed_actions", "sha_pinning_required") if key in value}

    def workflow_permissions(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("actions/permissions/workflow"), operation="workflow_permissions")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="workflow_permissions")
        return {key: value[key] for key in ("default_workflow_permissions", "can_approve_pull_request_reviews") if key in value}

    def selected_actions(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("actions/permissions/selected-actions"), operation="selected_actions")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="selected_actions")
        return {key: value[key] for key in ("github_owned_allowed", "verified_allowed", "patterns_allowed") if key in value}

    @staticmethod
    def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            value = row.get(key)
            label = value if isinstance(value, str) and value else "unknown"
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _pagination(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "complete": result["complete"],
            "pages": result["pages"],
            "reported_total": result["reported_total"],
        }

    def workflow_inventory(self) -> dict[str, Any]:
        result = self._paginate_result("actions/workflows", operation="workflow_inventory", collection_key="workflows")
        rows = result["items"]
        return {
            "value": {"count": len(rows), "state_counts": self._counts(rows, "state")},
            "pagination": self._pagination(result),
        }

    def workflow_runs_summary(self, branch: str) -> dict[str, Any]:
        suffix = f"actions/runs?branch={quote(branch, safe='')}"
        result = self._paginate_result(
            suffix,
            operation="workflow_runs",
            collection_key="workflow_runs",
            page_limit=10,
        )
        if len(result["items"]) >= 1000:
            result["complete"] = False
        rows = result["items"]
        return {
            "value": {
                "observed_run_count": len(rows),
                "reported_total": result["reported_total"],
                "status_counts": self._counts(rows, "status"),
                "conclusion_counts": self._counts(rows, "conclusion"),
            },
            "pagination": self._pagination(result),
        }

    def check_runs_summary(self, ref: str) -> dict[str, Any]:
        suffix = f"commits/{quote(ref, safe='')}/check-runs"
        result = self._paginate_result(suffix, operation="check_runs", collection_key="check_runs", page_limit=10)
        if len(result["items"]) >= 1000:
            result["complete"] = False
        rows = result["items"]
        return {
            "value": {
                "observed_check_run_count": len(rows),
                "reported_total": result["reported_total"],
                "status_counts": self._counts(rows, "status"),
                "conclusion_counts": self._counts(rows, "conclusion"),
            },
            "pagination": self._pagination(result),
        }

    def commit_status_summary(self, ref: str) -> dict[str, Any]:
        endpoint = self._repo_path(f"commits/{quote(ref, safe='')}/status")
        value = self._request("GET", endpoint, operation="commit_statuses")
        if not isinstance(value, dict) or not isinstance(value.get("state"), str):
            raise GitHubError(None, operation="commit_statuses")
        result = self._paginate_result(f"commits/{quote(ref, safe='')}/statuses", operation="commit_statuses_list")
        return {
            "value": {
                "aggregate_state": value["state"],
                "total_count": value.get("total_count") if isinstance(value.get("total_count"), int) else None,
                "observed_status_count": len(result["items"]),
                "status_counts": self._counts(result["items"], "state"),
            },
            "pagination": self._pagination(result),
        }

    def environments_summary(self) -> dict[str, Any]:
        result = self._paginate_result("deployments/environments", operation="environments")
        rows = result["items"]
        reviewer_counts = []
        for row in rows:
            rules = row.get("protection_rules")
            if isinstance(rules, list):
                reviewer_counts.append(sum(1 for rule in rules if isinstance(rule, dict) and rule.get("type") == "required_reviewers"))
        return {
            "value": {
                "count": len(rows),
                "names": sorted(row["name"] for row in rows if isinstance(row.get("name"), str)),
                "required_reviewer_rule_counts": sorted(reviewer_counts),
            },
            "pagination": self._pagination(result),
        }

    def deployments_summary(self) -> dict[str, Any]:
        result = self._paginate_result("deployments", operation="deployments")
        rows = result["items"]
        by_environment = self._counts(rows, "environment")
        latest = rows[0] if rows else None
        latest_summary = None
        statuses_complete = True
        if isinstance(latest, dict):
            latest_id = latest.get("id")
            if isinstance(latest_id, int) and not isinstance(latest_id, bool) and latest_id > 0:
                status_result = self._paginate_result(f"deployments/{latest_id}/statuses", operation="deployment_statuses")
                statuses = status_result["items"]
                statuses_complete = status_result["complete"]
                latest_status = statuses[0] if statuses else None
                latest_summary = {
                    "environment": latest.get("environment") if isinstance(latest.get("environment"), str) else None,
                    "created_at": latest.get("created_at") if isinstance(latest.get("created_at"), str) else None,
                    "status": latest_status.get("state") if isinstance(latest_status, dict) and isinstance(latest_status.get("state"), str) else None,
                    "status_created_at": latest_status.get("created_at") if isinstance(latest_status, dict) and isinstance(latest_status.get("created_at"), str) else None,
                    "status_pagination": self._pagination(status_result),
                }
        return {
            "value": {"count": len(rows), "environment_counts": by_environment, "latest": latest_summary},
            "pagination": {**self._pagination(result), "complete": result["complete"] and statuses_complete},
        }

    def pages_read(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("pages"), operation="pages_read")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="pages_read")
        return {key: value[key] for key in ("build_type", "source", "cname", "https_enforced", "status", "public") if key in value}

    def pages_health(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("pages/health"), operation="pages_health")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="pages_health")
        keys = ("status", "domain", "alt_domain", "is_https_eligible", "dns")
        return {key: value[key] for key in keys if key in value}

    def pages(self) -> dict[str, Any]:
        # A 404 is not enough evidence to treat Pages as disabled; callers may
        # update an observed Pages site, but must not plan creation from ambiguity.
        return self.pages_read()

    def _toggle(self, endpoint: str, operation: str) -> bool:
        try:
            self._request("GET", self._repo_path(endpoint), operation=operation)
            return True
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise

    def vulnerability_alerts_enabled(self) -> bool:
        return self._toggle("vulnerability-alerts", "dependabot_alerts_status")

    def automated_security_fixes_enabled(self) -> bool:
        return self._toggle("automated-security-fixes", "automated_security_fixes_status")

    def private_vulnerability_reporting_enabled(self) -> bool:
        value = self._request(
            "GET",
            self._repo_path("private-vulnerability-reporting"),
            operation="private_vulnerability_reporting_status",
        )
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise GitHubError(None, operation="private_vulnerability_reporting_status")
        return value["enabled"]

    def _alert_count(self, suffix: str, operation: str) -> int:
        rows = self._paginate(suffix, operation=operation)
        return len(rows)

    def dependabot_alert_count(self) -> int:
        return self._alert_count("dependabot/alerts?state=open", "dependabot_alerts")

    def code_scanning_alert_count(self) -> int:
        return self._alert_count("code-scanning/alerts?state=open", "code_scanning_alerts")

    def secret_scanning_alert_count(self) -> int:
        return self._alert_count("secret-scanning/alerts?state=open", "secret_scanning_alerts")

    def custom_secret_pattern_count(self) -> dict[str, Any]:
        result = self._paginate_result("secret-scanning/custom-patterns", operation="custom_secret_patterns")
        return {"value": {"count": len(result["items"])}, "pagination": self._pagination(result)}

    def sbom_summary(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("dependency-graph/sbom"), operation="sbom")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="sbom")
        sbom = value.get("sbom")
        if isinstance(sbom, str):
            if len(sbom.encode("utf-8")) > 10_000_000:
                raise GitHubError(None, operation="sbom")
            try:
                sbom = json.loads(sbom)
            except json.JSONDecodeError as exc:
                raise GitHubError(None, operation="sbom") from exc
        if not isinstance(sbom, dict):
            raise GitHubError(None, operation="sbom")
        packages = sbom.get("packages")
        if not isinstance(packages, list):
            raise GitHubError(None, operation="sbom")
        return {
            "format": sbom.get("spdxVersion") if isinstance(sbom.get("spdxVersion"), str) else None,
            "package_count": len(packages),
        }

    def immutable_releases_enabled(self) -> bool:
        value = self._request("GET", self._repo_path("immutable-releases"), operation="immutable_releases_status")
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise GitHubError(None, operation="immutable_releases_status")
        return value["enabled"]

    @staticmethod
    def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
        assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        return {
            "id": release.get("id") if isinstance(release.get("id"), int) and not isinstance(release.get("id"), bool) else None,
            "tag_name": release.get("tag_name") if isinstance(release.get("tag_name"), str) else None,
            "published_at": release.get("published_at") if isinstance(release.get("published_at"), str) else None,
            "draft": release.get("draft") if isinstance(release.get("draft"), bool) else None,
            "prerelease": release.get("prerelease") if isinstance(release.get("prerelease"), bool) else None,
            "immutable": release.get("immutable") if isinstance(release.get("immutable"), bool) else None,
            "embedded_asset_count": len(assets),
            "embedded_asset_digest_count": sum(
                1 for asset in assets if isinstance(asset, dict) and isinstance(asset.get("digest"), str) and asset["digest"]
            ),
        }

    def latest_release_summary(self) -> dict[str, Any]:
        value = self._request("GET", self._repo_path("releases/latest"), operation="latest_release")
        if not isinstance(value, dict):
            raise GitHubError(None, operation="latest_release")
        return self._release_summary(value)

    def release_inventory(self) -> dict[str, Any]:
        result = self._paginate_result("releases", operation="release_inventory")
        rows = result["items"]
        published = [row for row in rows if not row.get("draft") and isinstance(row.get("published_at"), str)]
        newest = max(published, key=lambda row: str(row.get("published_at")), default=None)
        newest_summary = self._release_summary(newest) if isinstance(newest, dict) else None
        return {
            "value": {
                "release_count": len(rows),
                "published_release_count": len(published),
                "draft_release_count": sum(1 for row in rows if row.get("draft") is True),
                "prerelease_count": sum(1 for row in rows if row.get("prerelease") is True),
                "newest_by_published_at": newest_summary,
                "latest_endpoint_ordering_may_differ": True,
            },
            "pagination": self._pagination(result),
        }

    def release_assets_summary(self) -> dict[str, Any]:
        releases = self._paginate_result("releases", operation="release_assets_releases")
        asset_count = 0
        digest_count = 0
        asset_pages = 0
        complete = releases["complete"]
        unavailable_release_count = 0
        asset_releases = releases["items"][:_RELEASE_ASSET_RELEASE_LIMIT]
        if len(releases["items"]) > _RELEASE_ASSET_RELEASE_LIMIT:
            complete = False
        for release in asset_releases:
            release_id = release.get("id")
            if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
                complete = False
                unavailable_release_count += 1
                continue
            result = self._paginate_result(
                f"releases/{release_id}/assets",
                operation="release_assets",
                page_limit=_RELEASE_ASSET_PAGE_LIMIT,
            )
            asset_pages += result["pages"]
            if not result["complete"]:
                complete = False
            asset_count += len(result["items"])
            digest_count += sum(
                1 for asset in result["items"] if isinstance(asset.get("digest"), str) and asset["digest"]
            )
        return {
            "value": {
                "release_count": len(releases["items"]),
                "asset_inventory_release_count": len(asset_releases),
                "asset_count": asset_count,
                "asset_digest_count": digest_count,
                "unavailable_release_count": unavailable_release_count,
            },
            "pagination": {
                "complete": complete,
                "release_pages": releases["pages"],
                "asset_pages": asset_pages,
                "release_reported_total": releases["reported_total"],
            },
        }

    def releases_summary(self) -> dict[str, Any]:
        releases = self._paginate("releases", operation="releases")
        published = [release for release in releases if not release.get("draft") and release.get("published_at")]
        published.sort(key=lambda release: str(release.get("published_at")), reverse=True)
        latest = None
        if published:
            release = published[0]
            assets = release.get("assets") if isinstance(release.get("assets"), list) else []
            latest = {
                "id": release.get("id"),
                "tag_name": release.get("tag_name"),
                "draft": bool(release.get("draft")),
                "prerelease": bool(release.get("prerelease")),
                "immutable": bool(release.get("immutable")),
                "asset_count": len(assets),
                "asset_digests_present": sum(1 for asset in assets if isinstance(asset, dict) and asset.get("digest")),
                "published_at": release.get("published_at"),
            }
        return {"release_count": len(releases), "published_release_count": len(published), "latest": latest}

    def social_preview_custom(self) -> bool:
        query = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { usesCustomOpenGraphImage } }"
        argv = [
            self._gh,
            "api",
            "--hostname",
            self.target.hostname,
            "graphql",
            "-F",
            f"owner={self._owner}",
            "-F",
            f"name={self._repo}",
            "-f",
            f"query={query}",
        ]
        try:
            stdout, stderr, returncode = _run_bounded_gh(argv, timeout=self._timeout)
        except _GhProcessFailure as exc:
            raise GitHubError(None, operation="social_preview_status") from exc
        if returncode:
            raise GitHubError(_status_from_output((stderr + stdout).decode("utf-8", errors="replace")), operation="social_preview_status")
        try:
            value = json.loads(stdout.decode("utf-8", errors="strict"))
            observed = value["data"]["repository"]["usesCustomOpenGraphImage"]
            if not isinstance(observed, bool):
                raise TypeError
            return observed
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise GitHubError(None, operation="social_preview_status") from exc

    def release_identity(self, release_id: int) -> dict[str, Any]:
        if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
            raise GitHubError(None, operation="release_identity")
        value = self._request("GET", self._repo_path(f"releases/{release_id}"), operation="release_identity")
        if not isinstance(value, dict) or value.get("id") != release_id or not isinstance(value.get("tag_name"), str):
            raise GitHubError(None, operation="release_identity")
        return {"id": release_id, "tag_name": value["tag_name"]}

    def release_asset_identity(self, release_id: int, asset_id: int) -> dict[str, Any]:
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (release_id, asset_id)):
            raise GitHubError(None, operation="release_asset_identity")
        value = self._request(
            "GET", self._repo_path(f"releases/{release_id}/assets/{asset_id}"),
            operation="release_asset_identity",
        )
        if not isinstance(value, dict) or value.get("id") != asset_id or not isinstance(value.get("name"), str):
            raise GitHubError(None, operation="release_asset_identity")
        digest = value.get("digest")
        if digest is not None and not isinstance(digest, str):
            raise GitHubError(None, operation="release_asset_identity")
        return {"id": asset_id, "name": value["name"], "digest": digest}

    def tag_commit_sha(self, tag_name: str) -> str:
        if not isinstance(tag_name, str) or not tag_name or len(tag_name) > 256:
            raise GitHubError(None, operation="release_tag_identity")
        ref = self._request(
            "GET", self._repo_path(f"git/ref/tags/{quote(tag_name, safe='')}"),
            operation="release_tag_identity",
        )
        if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag_name}":
            raise GitHubError(None, operation="release_tag_identity")
        object_value = ref.get("object")
        if not isinstance(object_value, dict) or not isinstance(object_value.get("sha"), str):
            raise GitHubError(None, operation="release_tag_identity")
        object_type = object_value.get("type")
        if object_type == "commit":
            return object_value["sha"]
        if object_type != "tag":
            raise GitHubError(None, operation="release_tag_identity")
        tag_object = self._request(
            "GET", self._repo_path(f"git/tags/{quote(object_value['sha'], safe='')}"),
            operation="release_tag_identity",
        )
        tagged_object = tag_object.get("object") if isinstance(tag_object, dict) else None
        if (
            not isinstance(tag_object, dict)
            or tag_object.get("tag") != tag_name
            or not isinstance(tagged_object, dict)
            or tagged_object.get("type") != "commit"
            or not isinstance(tagged_object.get("sha"), str)
        ):
            raise GitHubError(None, operation="release_tag_identity")
        return tagged_object["sha"]

    def _verification_commands_ready(self) -> bool:
        if self._verification_commands_available is not None:
            return self._verification_commands_available
        checks = (
            ([self._gh, "release", "verify-asset", "--help"], _RELEASE_VERIFY_ASSET_HELP),
            ([self._gh, "attestation", "verify", "--help"], _ATTESTATION_VERIFY_HELP),
        )
        try:
            for argv, expected_usage in checks:
                stdout, stderr, returncode = _run_bounded_gh(argv, timeout=10)
                if returncode != 0 or expected_usage.search(stdout + b"\n" + stderr) is None:
                    self._verification_commands_available = False
                    return False
        except _GhProcessFailure:
            self._verification_commands_available = False
            return False
        self._verification_commands_available = True
        return True

    @staticmethod
    def _verification_json(stdout: bytes) -> bool:
        try:
            value = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(value, (dict, list))

    def verify_release_asset(self, tag_name: str, file_path: str) -> dict[str, Any]:
        if not self._verification_commands_ready():
            return {"status": "unavailable", "reason": "gh_release_verify_asset_unavailable"}
        repo = f"{self.target.hostname}/{self.target.owner}/{self.target.repository}"
        try:
            stdout, _stderr, returncode = _run_bounded_gh(
                [self._gh, "release", "verify-asset", tag_name, file_path, "--repo", repo, "--format", "json"],
                timeout=self._timeout,
            )
        except _GhProcessFailure:
            return {"status": "unavailable", "reason": "gh_release_verify_asset_unavailable"}
        if returncode != 0:
            return {"status": "invalid", "reason": "release_asset_signature_verification_failed"}
        if not self._verification_json(stdout):
            return {"status": "unavailable", "reason": "release_asset_verification_result_invalid"}
        return {"status": "verified", "reason": None}

    def verify_attestation(
        self,
        file_path: str,
        *,
        signer_workflow: str,
        source_ref: str,
        predicate_type: str,
    ) -> dict[str, Any]:
        if not self._verification_commands_ready():
            return {"status": "unavailable", "reason": "gh_attestation_verify_unavailable"}
        repo = f"{self.target.hostname}/{self.target.owner}/{self.target.repository}"
        try:
            stdout, _stderr, returncode = _run_bounded_gh(
                [
                    self._gh, "attestation", "verify", file_path,
                    "--repo", repo,
                    "--signer-workflow", signer_workflow,
                    "--source-ref", source_ref,
                    "--predicate-type", predicate_type,
                    "--format", "json",
                ],
                timeout=self._timeout,
            )
        except _GhProcessFailure:
            return {"status": "unavailable", "reason": "gh_attestation_verify_unavailable"}
        if returncode != 0:
            return {"status": "invalid", "reason": "release_attestation_verification_failed"}
        if not self._verification_json(stdout):
            return {"status": "unavailable", "reason": "release_attestation_result_invalid"}
        # The proof is the fresh CLI verification under the identity flags above.
        # JSON predicate contents are workflow-controlled and are deliberately ignored.
        return {"status": "verified", "reason": None}

    def operation_state(self, operation: dict[str, Any]) -> Any:
        kind = operation.get("kind")
        desired = operation.get("desired")
        if kind == "repository_patch":
            repo = self.repository()
            return {key: repo.get(key) for key in desired}
        if kind == "repository_visibility":
            return self.repository().get("visibility")
        if kind == "repository_default_branch":
            desired = operation.get("desired")
            branch = desired.get("default_branch") if isinstance(desired, dict) else None
            if not isinstance(branch, str) or not branch:
                raise GitHubError(None, operation="repository_default_branch")
            repo = self.repository()
            return {
                "default_branch": repo.get("default_branch"),
                "target_branch": branch,
                "target_branch_sha": self.branch_head_sha(branch).lower(),
            }
        if kind == "topics_replace":
            return self.topics()
        if kind == "ruleset_upsert":
            if operation.get("resource_id") is None:
                key = self._ruleset_key(desired)
                ruleset_id = self._ruleset_create_ids.get(key)
                if ruleset_id is None:
                    matches = [rule for rule in self.rulesets() if rule.get("source_type") == "Repository" and rule.get("target") == key[1] and rule.get("name") == key[0]]
                    if len(matches) > 1:
                        raise GitHubError(None, operation="ambiguous_ruleset_name")
                    if not matches:
                        if key in self._ruleset_create_attempted:
                            raise GitHubError(None, operation="ruleset_detail_unavailable_after_create")
                        return None
                    ruleset_id = matches[0].get("id")
                    if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
                        raise GitHubError(None, operation="ruleset_detail_unavailable_after_create")
                    self._ruleset_create_ids[key] = ruleset_id
                return _checked_ruleset_detail(
                    self.get_ruleset(ruleset_id), ruleset_id, operation="ruleset_detail_unavailable_after_create"
                )
            ruleset_id = operation.get("resource_id")
            if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
                raise GitHubError(None, operation="repository_ruleset")
            return _checked_ruleset_detail(
                self.get_ruleset(ruleset_id), ruleset_id, operation="repository_ruleset_detail_unavailable"
            )
        if kind == "actions_repository_policy":
            return _required_state_fields(self.actions_permissions(), set(desired), operation="actions_permissions_state")
        if kind == "actions_workflow_policy":
            return _required_state_fields(self.workflow_permissions(), set(desired), operation="workflow_permissions_state")
        if kind == "actions_selected_policy":
            return _required_state_fields(self.selected_actions(), set(desired), operation="selected_actions_state")
        if kind in {"pages_create", "pages_update"}:
            current = self.pages()
            if current is None:
                raise GitHubError(None, operation="pages_state")
            return _required_state_fields(current, set(desired), operation="pages_state")
        if kind == "security_analysis_patch":
            repo = self.repository()
            security = repo.get("security_and_analysis")
            if not isinstance(security, dict):
                return {}
            return {
                key: ((security.get(key) or {}).get("status") == "enabled")
                if isinstance(security.get(key), dict) and (security.get(key) or {}).get("status") in {"enabled", "disabled"}
                else None
                for key in desired
            }
        if kind == "dependabot_alerts_toggle":
            return self.vulnerability_alerts_enabled()
        if kind == "automated_security_fixes_toggle":
            return self.automated_security_fixes_enabled()
        if kind == "private_vulnerability_reporting_toggle":
            return self.private_vulnerability_reporting_enabled()
        if kind == "immutable_releases_toggle":
            return self.immutable_releases_enabled()
        raise GitHubError(None, operation="unsupported_typed_operation")

    def _apply_repo_edit(self, operation: dict[str, Any], interface: dict[str, Any]) -> None:
        if self.target.hostname != "github.com":
            raise GitHubError(None, operation="native_gh_repo_edit_target_unavailable")
        argv_template = interface.get("argv_template")
        required_flags = interface.get("required_flags")
        environment_template = interface.get("environment")
        if (
            not isinstance(argv_template, list)
            or any(not isinstance(item, str) for item in argv_template)
            or not isinstance(required_flags, list)
            or not isinstance(environment_template, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment_template.items())
        ):
            raise GitHubError(None, operation="native_gh_repo_edit_interface_invalid")
        argv = [
            item.replace("{plan.target.requested_full_name}", self.target.full_name)
            for item in argv_template
        ]
        if not argv or argv[0] != "gh" or argv[1:3] != ["repo", "edit"] or "{plan.target." in " ".join(argv):
            raise GitHubError(None, operation="native_gh_repo_edit_interface_invalid")
        argv[0] = self._gh
        command_env = {
            key: value.replace("{plan.target.hostname}", self.target.hostname)
            for key, value in environment_template.items()
        }
        if any("{plan.target." in value for value in command_env.values()):
            raise GitHubError(None, operation="native_gh_repo_edit_interface_invalid")
        try:
            help_stdout, help_stderr, help_status = _run_bounded_gh(
                [self._gh, "repo", "edit", "--help"], timeout=min(self._timeout, 15.0)
            )
        except _GhProcessFailure as exc:
            raise GitHubError(None, operation="native_gh_repo_edit_help_unavailable") from exc
        help_text = help_stdout + b"\n" + help_stderr
        usage_is_specific = re.search(rb"(?im)^\s*gh(?:\.exe)?\s+repo\s+edit(?:\s|\[|$)", help_text) is not None
        flags_are_specific = all(
            re.search(
                rb"(?im)^\s*(?:-[A-Za-z],\s*)?" + re.escape(flag.encode("ascii")) + rb"(?:\s|,|=|$)",
                help_text,
            ) is not None
            for flag in required_flags
        )
        requires_boolean_false = any(re.fullmatch(r"--[a-z0-9-]+=false", item) for item in argv)
        false_syntax_is_documented = (
            not requires_boolean_false
            or re.search(rb"(?im)toggle\s+(?:a\s+)?setting\s+off.*--<flag>=false", help_text) is not None
        )
        if help_status != 0 or not usage_is_specific or not flags_are_specific or not false_syntax_is_documented:
            raise GitHubError(None, operation="native_gh_repo_edit_command_or_required_flag_unavailable")
        try:
            _stdout, stderr, returncode = _run_bounded_gh(argv, timeout=self._timeout, env=command_env)
        except _GhProcessFailure as exc:
            raise GitHubError(None, operation="native_gh_repo_edit", ambiguous=True) from exc
        if returncode:
            status = _status_from_output(stderr.decode("utf-8", errors="replace"))
            raise GitHubError(status, operation="native_gh_repo_edit", ambiguous=status is None)

    def apply_operation(self, operation: dict[str, Any]) -> None:
        kind = operation.get("kind")
        desired = operation.get("desired")
        try:
            expected_interface = describe_operation_interface(
                kind, operation.get("current"), desired, operation.get("resource_id")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(None, operation="typed_operation_interface_invalid") from exc
        interface = operation.get("interface", expected_interface)
        if interface != expected_interface:
            raise GitHubError(None, operation="typed_operation_interface_invalid")
        if interface["transport"] == "gh_repo_edit":
            self._apply_repo_edit(operation, interface)
            return
        if kind == "repository_patch":
            self._request("PATCH", self._repo_path(), body=desired, operation="repository_patch")
            return
        if kind == "repository_default_branch":
            if not isinstance(desired, dict) or not isinstance(desired.get("default_branch"), str):
                raise GitHubError(None, operation="repository_default_branch")
            self._request(
                "PATCH",
                self._repo_path(),
                body={"default_branch": desired["default_branch"]},
                operation="repository_default_branch",
            )
            return
        if kind == "repository_visibility":
            self._request("PATCH", self._repo_path(), body={"visibility": desired}, operation="repository_visibility")
            return
        if kind == "topics_replace":
            self._request("PUT", self._repo_path("topics"), body={"names": desired}, operation="topics_replace")
            return
        if kind == "ruleset_upsert":
            ruleset_id = operation.get("resource_id")
            if ruleset_id is None:
                key = self._ruleset_key(desired)
                self._ruleset_create_attempted.add(key)
                created = self._request("POST", self._repo_path("rulesets"), body=desired, operation="ruleset_create")
                if isinstance(created, dict):
                    created_id = created.get("id")
                    if isinstance(created_id, int) and not isinstance(created_id, bool) and created_id > 0:
                        self._ruleset_create_ids[key] = created_id
            else:
                self._request("PUT", self._repo_path(f"rulesets/{int(ruleset_id)}"), body=desired, operation="ruleset_update")
            return
        if kind == "actions_repository_policy":
            current = self.actions_permissions()
            if (
                not {"enabled", "allowed_actions", "sha_pinning_required"} <= set(current)
                or not isinstance(current.get("enabled"), bool)
                or not isinstance(current.get("allowed_actions"), str)
                or current["allowed_actions"] not in {"all", "local_only", "selected"}
                or not isinstance(current.get("sha_pinning_required"), bool)
            ):
                raise GitHubError(None, operation="actions_permissions_state_before_write")
            body = {**current, **desired}
            self._request("PUT", self._repo_path("actions/permissions"), body=body, operation="actions_repository_policy")
            return
        if kind == "actions_workflow_policy":
            self._request("PUT", self._repo_path("actions/permissions/workflow"), body=desired, operation="actions_workflow_policy")
            return
        if kind == "actions_selected_policy":
            self._request("PUT", self._repo_path("actions/permissions/selected-actions"), body=desired, operation="actions_selected_policy")
            return
        if kind in {"pages_create", "pages_update"}:
            method = "POST" if kind == "pages_create" else "PUT"
            self._request(method, self._repo_path("pages"), body=desired, operation=kind)
            return
        if kind == "security_analysis_patch":
            body = {"security_and_analysis": {key: {"status": "enabled" if enabled else "disabled"} for key, enabled in desired.items()}}
            self._request("PATCH", self._repo_path(), body=body, operation="security_analysis_patch")
            return
        toggle_paths = {
            "dependabot_alerts_toggle": "vulnerability-alerts",
            "automated_security_fixes_toggle": "automated-security-fixes",
            "private_vulnerability_reporting_toggle": "private-vulnerability-reporting",
            "immutable_releases_toggle": "immutable-releases",
        }
        if kind in toggle_paths:
            enabled = bool(desired)
            method = "PUT" if enabled else "DELETE"
            self._request(method, self._repo_path(toggle_paths[kind]), operation=kind)
            return
        raise GitHubError(None, operation="unsupported_typed_operation")

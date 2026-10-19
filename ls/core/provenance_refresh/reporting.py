"""Turn verified Herdr observations into compact, stable issue findings."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


HERDR_COMMIT_URL = "https://github.com/herdrdev/herdr/commit/"
HERDR_SOURCE_URL = "https://github.com/herdrdev/herdr/blob/"
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SOURCE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

LOCAL_OWNER_PATHS = [
    "ls/core/herdr_ops/",
    "ls/skills/ls-herdr/references/upstream/provenance.json",
    "ls/skills/ls-herdr-provenance-maintenance/SKILL.md",
]


def build_findings(report: dict[str, Any], *, run_url: str | None = None) -> list[dict[str, Any]]:
    """Build at most one source handoff from fully validated evidence."""
    upstream = report.get("upstream")
    source_watch = report.get("source_watch")
    release_evidence = upstream.get("evidence") if isinstance(upstream, dict) else None
    source_evidence = source_watch.get("evidence") if isinstance(source_watch, dict) else None
    release = _actionable_release(release_evidence)
    changed_files, matching_files, unknown_source_count, source_revision = _confirmed_file_drift(source_evidence)
    if release is None and not changed_files:
        return []

    unknown_observations = [
        summary
        for summary in (
            _release_unknown_summary(release_evidence),
            _source_unknown_summary(source_evidence, unknown_source_count),
        )
        if summary is not None
    ]

    current_release = _confirmed_current_release(release_evidence) if release is None else None
    confirmed_release = release or current_release
    pinned = confirmed_release["pinned"] if confirmed_release else None
    observed = confirmed_release["observed"] if confirmed_release else None
    evidence_identity: dict[str, Any] = {
        "pinned_release_tag": pinned.get("tag") if isinstance(pinned, dict) else None,
        "observed_release_tag": observed.get("tag") if isinstance(observed, dict) else None,
        # Matching observations are metadata for retiring earlier drift. The
        # visible body below still describes only drift confirmed in this run.
        "source_files": [
            {
                "path": item["source_path"],
                "pinned_sha256": item["pinned_sha256"],
                "observed_sha256": item["observed_sha256"],
            }
            for item in changed_files + matching_files
        ],
    }
    body = _render_body(
        release=release,
        changed_files=changed_files,
        source_revision=source_revision,
        unknown_observations=unknown_observations,
        run_url=_safe_run_url(run_url),
    )
    title = "Herdr upstream evidence needs review"
    return [
        {
            "category": "provenance",
            "check_type": "herdr_upstream_provenance",
            "severity": "medium",
            "title": title,
            "affected_paths": LOCAL_OWNER_PATHS.copy(),
            "region": "herdr-upstream",
            "evidence_identity": evidence_identity,
            "body": body,
        }
    ]


def _actionable_release(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, dict) or evidence.get("status") != "newer_stable":
        return None
    pinned = evidence.get("pinned")
    observed = evidence.get("observed")
    if not isinstance(pinned, dict) or not isinstance(observed, dict):
        return None
    if (
        _stable_version(pinned.get("version")) is None
        or _stable_version(observed.get("version")) is None
        or not _git_sha(pinned.get("commit"))
        or not _git_sha(observed.get("commit"))
        or not _immutable_commit_url(pinned.get("immutable_url"), pinned["commit"])
        or not _immutable_commit_url(observed.get("immutable_url"), observed["commit"])
    ):
        return None
    if not isinstance(pinned.get("tag"), str) or not isinstance(observed.get("tag"), str):
        return None
    if (
        _stable_version(pinned["tag"]) != _stable_version(pinned["version"])
        or _stable_version(observed["tag"]) != _stable_version(observed["version"])
    ):
        return None
    return {"pinned": pinned, "observed": observed}


def _confirmed_current_release(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, dict) or evidence.get("status") != "current":
        return None
    pinned = evidence.get("pinned")
    observed = evidence.get("observed")
    if not isinstance(pinned, dict) or not isinstance(observed, dict):
        return None
    pinned_version = _stable_version(pinned.get("version"))
    observed_version = _stable_version(observed.get("version"))
    if (
        pinned_version is None
        or observed_version is None
        or _stable_version(pinned.get("tag")) != pinned_version
        or _stable_version(observed.get("tag")) != observed_version
        or tuple(map(int, observed_version.split("."))) > tuple(map(int, pinned_version.split(".")))
    ):
        return None
    return {"pinned": pinned, "observed": observed}


def _confirmed_file_drift(evidence: Any) -> tuple[list[dict[str, str]], list[dict[str, str]], int, dict[str, str] | None]:
    if not isinstance(evidence, dict):
        return [], [], 0, None
    unknown = evidence.get("unknown")
    unknown_count = len(unknown) if isinstance(unknown, list) else 0
    files = evidence.get("files")
    if not isinstance(files, list):
        return [], [], unknown_count, None
    status = evidence.get("status")
    if status not in {"current", "drift", "unknown"}:
        return [], [], max(1, unknown_count), None
    revision = evidence.get("revision")
    if not _git_sha(revision) or not _immutable_commit_url(evidence.get("immutable_url"), revision):
        return [], [], max(1, unknown_count), None
    pinned_revision = evidence.get("pinned_revision")
    pinned_link = evidence.get("pinned_immutable_url")
    revision_info = None
    if _git_sha(pinned_revision) and _immutable_commit_url(pinned_link, pinned_revision):
        revision_info = {
            "pinned": pinned_revision,
            "pinned_url": pinned_link,
            "observed": revision,
            "observed_url": evidence["immutable_url"],
            "compare_url": f"https://github.com/herdrdev/herdr/compare/{pinned_revision}...{revision}",
        }
    confirmed: list[dict[str, str]] = []
    matching: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        source_path = item.get("source_path")
        pinned = item.get("pinned_sha256")
        observed = item.get("observed_sha256")
        link = item.get("immutable_url")
        if (
            not _safe_source_path(source_path)
            or not _SHA256(pinned)
            or not _SHA256(observed)
            or not _immutable_source_url(link, revision, source_path)
        ):
            continue
        observation = {
            "source_path": source_path,
            "pinned_sha256": pinned,
            "observed_sha256": observed,
            "immutable_url": link,
        }
        if pinned == observed:
            matching.append(observation)
        elif status == "drift":
            confirmed.append(observation)
    return confirmed, matching, unknown_count, revision_info


def _release_unknown_summary(evidence: Any) -> str | None:
    if not isinstance(evidence, dict):
        return "Release freshness could not be confirmed."
    status = evidence.get("status")
    if status in {"current", "newer_stable"}:
        return None
    if status == "unstable_or_unknown":
        return "Release freshness was unknown because the latest metadata did not confirm a stable release."
    reason = evidence.get("reason")
    if isinstance(reason, str) and reason.startswith("official metadata request failed"):
        return "Official release metadata could not be read or timed out."
    if isinstance(reason, str) and reason.startswith("official stable release commit"):
        return "The stable release tag could not be resolved to a full immutable commit."
    if isinstance(reason, str) and reason.startswith("required local release pins"):
        return "The local stable release pins were incomplete or invalid."
    return "Release freshness could not be confirmed."


def _source_unknown_summary(evidence: Any, unknown_count: int) -> str | None:
    if unknown_count:
        return f"{unknown_count} other watched source observation(s) were unknown and are not asserted here."
    if not isinstance(evidence, dict):
        return "Watched source-file freshness could not be confirmed."
    status = evidence.get("status")
    if status in {"current", "drift"}:
        return None
    reason = evidence.get("reason")
    if isinstance(reason, str) and reason.startswith("official metadata request failed"):
        return "Watched source files could not be read from the official repository or timed out."
    return "Watched source-file freshness could not be confirmed."


def _render_body(
    *,
    release: dict[str, Any] | None,
    changed_files: list[dict[str, str]],
    source_revision: dict[str, str] | None,
    unknown_observations: list[str],
    run_url: str | None,
) -> str:
    lines = ["A bounded check of Herdr's official release metadata and watched source files found reviewable drift.", "", "## Confirmed evidence"]
    if release:
        pinned = release["pinned"]
        observed = release["observed"]
        lines.extend(
            [
                f"- Stable release: pinned `{pinned['version']}` (`{pinned['tag']}`), observed `{observed['version']}` (`{observed['tag']}`).",
                f"- Pinned revision: [`{pinned['commit']}`]({pinned['immutable_url']}).",
                f"- Observed revision: [`{observed['commit']}`]({observed['immutable_url']}).",
            ]
        )
        if not observed.get("sha256"):
            lines.append("- Release checksums were unavailable; the stable version and immutable release commit are confirmed.")
    if changed_files:
        if source_revision:
            lines.extend(
                [
                    f"- Pinned source revision: [`{source_revision['pinned']}`]({source_revision['pinned_url']}).",
                    f"- Observed source revision: [`{source_revision['observed']}`]({source_revision['observed_url']}).",
                    f"- [Compare these immutable revisions]({source_revision['compare_url']}).",
                ]
            )
    for item in changed_files:
        lines.append(
            f"- Watched file `{item['source_path']}`: `{item['pinned_sha256']}` → `{item['observed_sha256']}` ([immutable upstream file]({item['immutable_url']}))."
        )
    if run_url:
        lines.extend([f"- Observation run: [workflow run]({run_url})."])
    if unknown_observations:
        lines.extend(["", "## Unconfirmed observations", *[f"- {summary}" for summary in unknown_observations]])
    lines.extend(
        [
            "",
            "## Local owner paths",
            *[f"- `{path}`" for path in LOCAL_OWNER_PATHS],
            "",
            "## Review handoff",
            "Review the pinned and observed official evidence, inspect the affected LocalSetup owners, and decide whether a compatible source or documentation update is needed. Validate any accepted update through normal review and tests.",
            "",
            'Focused validation: `workers="$(uv run --locked python ls/tools/localsetup.py --source-root . test-workers)" && uv run --locked pytest -n "$workers" ls/tests/test_herdr_ops_provenance.py ls/tests/test_provenance_refresh.py ls/tests/test_qc_patrol.py -q`.',
            "",
            "This handoff gives no authority to upgrade a Herdr host or change a running service.",
        ]
    )
    return "\n".join(lines)


def _stable_version(value: Any) -> str | None:
    return value.removeprefix("v") if isinstance(value, str) and _STABLE_VERSION_RE.fullmatch(value) else None


def _git_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_GIT_SHA_RE.fullmatch(value))


def _SHA256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _safe_source_path(value: Any) -> bool:
    if not isinstance(value, str) or not _SOURCE_PATH_RE.fullmatch(value) or "\\" in value:
        return False
    parts = value.split("/")
    return bool(value) and not value.startswith("/") and all(part not in {"", ".", ".."} for part in parts)


def _immutable_commit_url(value: Any, commit: str) -> bool:
    return value == f"{HERDR_COMMIT_URL}{commit}"


def _immutable_source_url(value: Any, revision: str, path: str) -> bool:
    return value == f"{HERDR_SOURCE_URL}{revision}/{path}"


def _safe_run_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 500:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.netloc != "github.com":
        return None
    if parsed.path.count("/") != 5 or not parsed.path.startswith("/CruxExperts/LocalSetup/actions/runs/"):
        return None
    if not parsed.path.rsplit("/", 1)[-1].isdigit():
        return None
    if parsed.query or parsed.fragment:
        return None
    return value

"""Focused read-only adapter contract tests for policy-stitch ownership."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import textwrap

import pytest

from ls.core.provenance_refresh import policy


def _owner_fixture(tmp_path: Path, report: dict | None = None, *, returncode: int = 0,
                   body: str | None = None) -> tuple[Path, Path]:
    owner = tmp_path / "project" / "scripts" / "ops" / "policy_stitch.py"
    owner.parent.mkdir(parents=True)
    registry = tmp_path / "binding" / "policy-stitch.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text("registry is passed through the fixed check command\n", encoding="utf-8")
    expected_argv = ["--registry", str(registry.resolve()), "--check", "--json", "--target", "all"]
    if body is None:
        payload = json.dumps(report if report is not None else _aligned_report())
        body = (
            "import json, sys\n"
            f"if sys.argv[1:] != {expected_argv!r}:\n"
            "    print('{}')\n"
            "    raise SystemExit(99)\n"
            f"print({payload!r})\n"
            f"raise SystemExit({returncode})\n"
        )
    owner.write_text(textwrap.dedent(body), encoding="utf-8")
    owner.chmod(0o644)
    registry.chmod(0o600)
    return owner, registry


def _aligned_report() -> dict:
    return {
        "schema_version": 1,
        "global": {"source": "/private/source", "content_sha256": "a" * 64, "content_lines": 12},
        "orientation": {
            "heading": "LocalSetup toolchain",
            "source": "global",
            "status": "ok",
            "content_sha256": "b" * 64,
            "content_lines": 5,
        },
        "targets": [
            {
                "id": "codex",
                "status": "noop",
                "action": "noop",
                "target": "/private/target",
                "content_sha256": "c" * 64,
                "head_sha256": "d" * 64,
                "orientation_sha256": "e" * 64,
            }
        ],
        "summary": {"noop": 1, "would_create": 0, "would_update": 0, "refused": 0},
    }


def test_inspect_policy_owner_reports_aligned_hashes_without_claiming_client_load(tmp_path: Path) -> None:
    owner, registry = _owner_fixture(tmp_path)
    owner_before = owner.read_bytes()
    registry_before = registry.read_bytes()

    result = policy.inspect_policy_owner(owner, registry)

    assert result["status"] == "aligned"
    assert result["loaded_by_client"] == "unverified"
    assert result["orientation"]["content_sha256"] == "b" * 64
    assert result["targets"][0]["orientation_sha256"] == "e" * 64
    assert "target" not in result["targets"][0]
    assert owner.read_bytes() == owner_before
    assert registry.read_bytes() == registry_before
    assert not (tmp_path / "project" / ".agents" / "state").exists()


def test_explicit_binding_absence_and_missing_paths_are_distinct(tmp_path: Path) -> None:
    assert policy.inspect_policy_owner(None, None) == {
        "status": "unavailable", "loaded_by_client": "unverified", "reason": "notconfigured"
    }
    owner, registry = _owner_fixture(tmp_path)
    assert policy.inspect_policy_owner(owner, None)["reason"] == "notconfigured"
    registry.unlink()
    assert policy.inspect_policy_owner(owner, registry)["reason"] == "missing"
    assert not registry.exists()


@pytest.mark.parametrize(
    ("status", "returncode", "report", "expected_reason"),
    [
        (
            "drift", 1,
            {**_aligned_report(), "targets": [{"id": "codex", "status": "would_update", "action": "update"}],
             "summary": {"noop": 0, "would_create": 0, "would_update": 1, "refused": 0}},
            "target_drift",
        ),
        (
            "refused", 2,
            {"schema_version": 1, "global": None, "targets": [],
             "summary": {"noop": 0, "would_create": 0, "would_update": 0, "refused": 1},
             "error": "private diagnostic must not be returned"},
            "owner_refused",
        ),
    ],
)
def test_drift_and_refusal_are_classified_without_raw_owner_diagnostics(
    tmp_path: Path, status: str, returncode: int, report: dict, expected_reason: str
) -> None:
    owner, registry = _owner_fixture(tmp_path, report, returncode=returncode)

    result = policy.inspect_policy_owner(owner, registry)

    assert result["status"] == status
    assert result["reason"] == expected_reason
    assert result["loaded_by_client"] == "unverified"
    assert "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize(
    "report",
    ["not-json", {"schema_version": 7}, {**_aligned_report(), "global": {"content_sha256": "bad"}}],
)
def test_malformed_owner_report_is_invalid(tmp_path: Path, report: object) -> None:
    if isinstance(report, str):
        body = f"print({report!r})\n"
    else:
        body = f"import json\nprint({json.dumps(report)!r})\n"
    owner, registry = _owner_fixture(tmp_path, body=body)

    result = policy.inspect_policy_owner(owner, registry)

    assert result["status"] == "invalid"
    assert result["loaded_by_client"] == "unverified"


def test_unsafe_owner_paths_are_refused(tmp_path: Path) -> None:
    owner, registry = _owner_fixture(tmp_path)
    owner.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IWGRP)

    result = policy.inspect_policy_owner(owner, registry)

    assert result["status"] == "refused"
    assert result["reason"] == "writable_by_others"


def test_owner_must_match_the_registered_script_boundary(tmp_path: Path) -> None:
    owner, registry = _owner_fixture(tmp_path)
    outside = tmp_path / "unrelated" / "policy_stitch.py"
    outside.parent.mkdir()
    outside.write_bytes(owner.read_bytes())
    outside.chmod(0o644)

    result = policy.inspect_policy_owner(outside, registry)

    assert result["status"] == "refused"
    assert result["reason"] == "outside_owner_boundary"


def test_owner_timeout_and_output_limit_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, registry = _owner_fixture(
        tmp_path,
        body="import time\ntime.sleep(5)\n",
    )
    monkeypatch.setattr(policy, "_OWNER_TIMEOUT_SECONDS", 0.05)
    result = policy.inspect_policy_owner(owner, registry)
    assert result["status"] == "invalid"
    assert result["reason"] == "timeout"

    owner.write_text("print('x' * 300000)\n", encoding="utf-8")
    result = policy.inspect_policy_owner(owner, registry)
    assert result["status"] == "invalid"
    assert result["reason"] == "output_limit"

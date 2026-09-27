from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from ls.core.github_repo import ci_evidence


REPOSITORY = "CruxExperts/LocalSetup"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _workflow() -> dict[str, Any]:
    return {"id": 42, "path": ".github/workflows/pr-validation.yml"}


def _run(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": 101,
        "workflow_id": 42,
        "head_sha": SHA,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "event": "pull_request",
        "path": ".github/workflows/pr-validation.yml",
        "created_at": "2026-09-25T12:00:00Z",
        "status": "completed",
        "conclusion": "success",
    }
    value.update(changes)
    return value


def _job(name: str, *, status: str = "completed", conclusion: str = "success") -> dict[str, str]:
    return {"name": name, "status": status, "conclusion": conclusion}


def _install_api(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, Any]) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        endpoint = command[-1]
        payload = payloads[endpoint]
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, dict):
            records = [payload]
        else:
            records = list(payload)
        stdout = "".join(json.dumps(record) + "\n" for record in records)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(ci_evidence.subprocess, "run", fake_run)
    return commands


def _payloads(runs: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"repos/{REPOSITORY}/actions/workflows/pr-validation.yml": _workflow(),
        f"repos/{REPOSITORY}/actions/workflows/pr-validation.yml/runs?head_sha={SHA}&per_page=100": runs,
        f"repos/{REPOSITORY}/actions/runs/101/jobs?per_page=100": jobs,
    }


def test_exact_successful_workflow_and_required_jobs_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _install_api(
        monkeypatch,
        _payloads([_run()], [_job("validate"), _job("test")]),
    )

    result = ci_evidence.check_ci_evidence(REPOSITORY, SHA, required_jobs=("validate", "test"))

    assert result == {
        "ok": True,
        "runs": [{
            "workflow": "pr-validation.yml",
            "run_id": 101,
            "head_sha": SHA,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "jobs": {
                "validate": {"status": "completed", "conclusion": "success"},
                "test": {"status": "completed", "conclusion": "success"},
            },
        }],
    }
    assert all(command[:2] == ["gh", "api"] for command in commands)
    assert all("--paginate" in command for command in commands if "/runs?" in command[-1] or "/jobs?" in command[-1])
    assert "--paginate" not in commands[0]


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "f" * 40},
        {"repository": {"full_name": "someone/else"}},
        {"head_repository": {"full_name": "someone/else"}},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"event": "schedule"},
        {"path": ".github/workflows/other.yml"},
    ],
)
def test_wrong_commit_repository_or_run_identity_is_not_evidence(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    _install_api(monkeypatch, _payloads([_run(**change)], []))

    result = ci_evidence.check_ci_evidence(REPOSITORY, SHA)

    assert result == {"ok": False, "runs": []}


def test_workflow_path_mismatch_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _payloads([_run()], [])
    payloads[f"repos/{REPOSITORY}/actions/workflows/pr-validation.yml"] = {
        "id": 42,
        "path": ".github/workflows/renamed.yml",
    }
    _install_api(monkeypatch, payloads)

    assert ci_evidence.check_ci_evidence(REPOSITORY, SHA)["ok"] is False


@pytest.mark.parametrize(
    "jobs",
    [
        [_job("validate")],
        [_job("validate"), _job("test", status="in_progress", conclusion="success")],
        [_job("validate"), _job("test", conclusion="failure")],
        [_job("validate"), _job("test"), _job("test")],
    ],
)
def test_required_jobs_must_each_be_unique_completed_successes(
    monkeypatch: pytest.MonkeyPatch, jobs: list[dict[str, Any]]
) -> None:
    _install_api(monkeypatch, _payloads([_run()], jobs))

    result = ci_evidence.check_ci_evidence(REPOSITORY, SHA, required_jobs=("validate", "test"))

    assert result == {"ok": False, "runs": []}


def test_excluded_run_id_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_api(monkeypatch, _payloads([_run()], []))

    result = ci_evidence.check_ci_evidence(REPOSITORY, SHA, exclude_run_id=101)

    assert result == {"ok": False, "runs": []}


def test_newer_failed_run_masks_an_older_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    older_success = _run(id=101, created_at="2026-09-24T12:00:00Z")
    newer_failure = _run(
        id=102,
        created_at="2026-09-25T12:00:00Z",
        status="completed",
        conclusion="failure",
    )
    _install_api(
        monkeypatch,
        _payloads([older_success, newer_failure], [_job("validate")]),
    )

    result = ci_evidence.check_ci_evidence(REPOSITORY, SHA, required_jobs=("validate",))

    assert result == {"ok": False, "runs": []}


@pytest.mark.parametrize("required", [False, True])
def test_timeout_is_sanitized_and_cli_exit_depends_on_require(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], required: bool
) -> None:
    endpoint = f"repos/{REPOSITORY}/actions/workflows/pr-validation.yml"
    _install_api(monkeypatch, {endpoint: subprocess.TimeoutExpired("gh", timeout=1, stderr="GH_TOKEN=private")})

    argv = ["--repository", REPOSITORY, "--sha", SHA]
    if required:
        argv.append("--require")
    result = ci_evidence.main(argv)

    printed = capsys.readouterr().out
    assert json.loads(printed) == {"ok": False, "runs": []}
    assert "GH_TOKEN" not in printed
    assert result == int(required)


def test_invalid_inputs_fail_closed_without_gh_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_call(*args: Any, **kwargs: Any) -> None:
        pytest.fail("gh must not run for invalid inputs")

    monkeypatch.setattr(ci_evidence.subprocess, "run", no_call)

    assert ci_evidence.check_ci_evidence(REPOSITORY, "short") == {"ok": False, "runs": []}
    assert ci_evidence.check_ci_evidence(REPOSITORY, SHA, required_jobs=("test", "test")) == {
        "ok": False,
        "runs": [],
    }

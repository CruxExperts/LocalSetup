from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ls.core.github_repo.adapter import GhCliAdapter, GitHubError
from ls.core.github_repo.maintenance_config import (
    CONFIG_RELATIVE_PATH,
    WORKFLOW_RELATIVE_PATH,
    MaintenanceError,
    canonical_config_bytes,
    parse_config_bytes,
    render_shared_workflow,
)
from ls.core.github_repo.maintenance_discovery import (
    PublicRepository,
    discover_checkouts,
    discover_public_repositories,
    origin_repository,
)
from ls.core.github_repo.maintenance_run import _issue_snapshot
from ls.core.github_repo.maintenance_service import _security_advisory_counts
from ls.core.github_repo.maintenance_setup import apply_setup, plan_setup
from ls.core.github_repo.maintenance_cli import _validate_selectors
import ls.core.github_repo.maintenance_run as run_module
import ls.core.github_repo.maintenance_service as service_module
import ls.core.github_repo.maintenance_setup as setup_service_module
from ls.core.github_repo.model import RepositoryTarget


_SHA = "a" * 40


def _config(repository_id: int = 123, full_name: str = "Acme/Widget") -> dict[str, object]:
    return {
        "schema": 1,
        "hostname": "github.com",
        "repository_id": repository_id,
        "full_name": full_name,
        "workflow_commit": _SHA,
        "validation_owners": ["ci.yml", "pr-validation.yml"],
        "adapters": {
            "python-stdlib": [
                {"name": "tests", "cwd": ".", "argv": ["python3", "-m", "unittest"], "timeout_seconds": 120}
            ]
        },
    }


class FakeAdapter:
    def __init__(self, target: RepositoryTarget, *, repository_id: int = 123) -> None:
        self.target = target
        self.repository_id = repository_id
        self.endpoints: list[str] = []

    def repository(self) -> dict[str, object]:
        return {"id": self.repository_id, "full_name": self.target.full_name, "default_branch": "main"}

    def branch_head_sha(self, _branch: str) -> str:
        return _SHA

    def dependabot_alert_count(self) -> int:
        return 0

    def code_scanning_alert_count(self) -> int:
        return 0

    def secret_scanning_alert_count(self) -> int:
        return 0

    def _request(self, _method: str, endpoint: str, *, operation: str) -> object:
        self.endpoints.append(endpoint)
        if endpoint == "users/Acme":
            return {"id": 7, "login": "Acme", "type": "Organization"}
        if endpoint == "users/alice":
            return {"id": 8, "login": "alice", "type": "User"}
        if endpoint.startswith("orgs/Acme/repos?"):
            page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
            if page == 1:
                return [
                    {"id": index, "full_name": f"Acme/Widget{index}", "private": False, "default_branch": "main"}
                    for index in range(1, 101)
                ]
            if page == 2:
                return [{"id": 123, "full_name": "Acme/Widget", "private": False, "default_branch": "main"}]
            return []
        if endpoint.startswith("users/alice/repos?"):
            return [{"id": 234, "full_name": "alice/example", "private": False, "default_branch": "main"}]
        raise AssertionError(f"unexpected API endpoint for {operation}: {endpoint}")


def _factory(target: RepositoryTarget) -> FakeAdapter:
    return FakeAdapter(target)


def _setup_args(checkout: Path, config: Path, *, apply: bool = False, authorize_plan: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        hostname="github.com",
        repository="Acme/Widget",
        checkout=str(checkout),
        config=str(config),
        apply=apply,
        authorize_plan=authorize_plan,
        all_public=False,
        owner=None,
        checkout_root=None,
        config_directory=None,
    )


def _checkout_with_origin(path: Path, full_name: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/{full_name}.git"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_config_schema_is_strict_and_command_argv_cannot_invoke_shell_or_inline_python() -> None:
    config = _config()
    assert parse_config_bytes(canonical_config_bytes(config)) == config
    wrapper = render_shared_workflow(config).decode()
    assert f"github-maintenance.yml@{_SHA}" in wrapper
    assert 'expected-repository-id: "123"' in wrapper
    assert "report-issues: false" in wrapper
    assert "external-tool-packages-json: '[]'" in wrapper
    assert "  push:" in wrapper
    assert "  pull_request:" in wrapper
    assert "  merge_group:" in wrapper
    assert "  workflow_dispatch:" in wrapper
    assert "    - cron: '3 2 * * *'" in wrapper

    scheduled = _config()
    scheduled["advisory_schedule"] = "15 6 * * *"
    scheduled["external_tool_packages"] = [{"ecosystem": "npm", "name": "eslint", "version": "9.0.0"}]
    scheduled_wrapper = render_shared_workflow(scheduled).decode()
    assert "    - cron: '15 6 * * *'" in scheduled_wrapper
    assert 'external-tool-packages-json: \'[{"ecosystem":"npm","name":"eslint","version":"9.0.0"}]\'' in scheduled_wrapper

    disabled_schedule = _config()
    disabled_schedule["advisory_schedule"] = None
    assert "schedule:" not in render_shared_workflow(disabled_schedule).decode()

    for malformed in (
        {**config, "unexpected": True},
        {**config, "validation_owners": ["tests"]},
        {**config, "advisory_schedule": "*/5 * * * *"},
        {**config, "external_tool_packages": [{"ecosystem": "npm", "name": "eslint", "version": "9.0.0", "command": "npm install"}]},
        {**config, "external_tool_packages": [{"ecosystem": "npm", "name": "eslint", "version": "9.0.0"}] * 501},
        {**config, "adapters": {"compose": [{"name": "bad", "cwd": ".", "argv": ["docker", "compose"], "timeout_seconds": 5}]}},
        {**config, "adapters": {"python-stdlib": [{"name": "bad", "cwd": ".", "argv": ["python3", "-c", "print(1)"], "timeout_seconds": 5}]}},
        {**config, "adapters": {"python-stdlib": [{"name": "bad", "cwd": ".", "argv": ["python3", "--token", "literal-secret"], "timeout_seconds": 5}]}},
        {**config, "adapters": {"python-stdlib": [{"name": "bad", "cwd": "../escape", "argv": ["python3", "-m", "unittest"], "timeout_seconds": 5}]}},
    ):
        with pytest.raises(MaintenanceError):
            parse_config_bytes(json.dumps(malformed).encode())


def test_public_repository_discovery_detects_user_or_org_and_pages_every_result() -> None:
    instances: list[FakeAdapter] = []

    def factory(target: RepositoryTarget) -> FakeAdapter:
        instance = FakeAdapter(target)
        instances.append(instance)
        return instance

    org_rows = discover_public_repositories("github.com", "Acme", adapter_factory=factory)
    assert len(org_rows) == 101
    assert any(row.full_name == "Acme/Widget" for row in org_rows)
    assert instances[-1].endpoints[0] == "users/Acme"
    assert instances[-1].endpoints[1].startswith("orgs/Acme/repos?")
    assert "page=2" in instances[-1].endpoints[2]

    user_rows = discover_public_repositories("github.com", "alice", adapter_factory=factory)
    assert user_rows[0].full_name == "alice/example"
    assert instances[-1].endpoints[1].startswith("users/alice/repos?")


def test_origin_discovery_uses_origin_only_and_rejects_credentials(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/Someone/Other.git"], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "upstream", "https://github.com/Acme/Widget.git"], check=True)

    mapping = discover_checkouts(tmp_path, "github.com")
    assert "someone/other" in mapping
    assert "acme/widget" not in mapping
    assert origin_repository("git@github.com:Acme/Widget.git", "github.com") == "Acme/Widget"
    assert origin_repository("ssh://git@github.com/Acme/Widget.git", "github.com") == "Acme/Widget"
    assert origin_repository("https://user:token@github.com/Acme/Widget.git", "github.com") is None


def test_setup_plan_is_identity_bound_private_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config_path = tmp_path / "reviewed.json"
    config_path.write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(setup_service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(setup_service_module, "_unexpected_tracked_maintenance_files", lambda *_: [])
    args = _setup_args(checkout, config_path)

    plan = plan_setup(args, tmp_path / "home", adapter_factory=_factory)
    assert plan["status"] == "planned"
    assert plan["unchanged"] is False
    assert not (checkout / CONFIG_RELATIVE_PATH).exists()
    assert "before_hashes" in plan

    applied = apply_setup(_setup_args(checkout, config_path, apply=True, authorize_plan=plan["plan_digest"]), tmp_path / "home", adapter_factory=_factory)
    assert applied["status"] == "complete"
    assert set(applied["changed_paths"]) == {CONFIG_RELATIVE_PATH, WORKFLOW_RELATIVE_PATH}
    assert (checkout / CONFIG_RELATIVE_PATH).read_bytes() == config_path.read_bytes()
    assert (checkout / WORKFLOW_RELATIVE_PATH).read_bytes() == render_shared_workflow(_config())

    repeated = apply_setup(_setup_args(checkout, config_path, apply=True, authorize_plan=plan["plan_digest"]), tmp_path / "home", adapter_factory=_factory)
    assert repeated["status"] == "already_applied"
    assert repeated["changed_paths"] == []


def test_setup_refuses_identity_change_and_post_review_preimage_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config_path = tmp_path / "reviewed.json"
    config_path.write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(setup_service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(setup_service_module, "_unexpected_tracked_maintenance_files", lambda *_: [])
    args = _setup_args(checkout, config_path)
    with pytest.raises(setup_service_module.MaintenanceOperationError, match="immutable repository ID"):
        plan_setup(args, tmp_path / "wrong-id-home", adapter_factory=lambda target: FakeAdapter(target, repository_id=999))

    plan = plan_setup(args, tmp_path / "home", adapter_factory=_factory)
    workflow = checkout / WORKFLOW_RELATIVE_PATH
    workflow.parent.mkdir(parents=True)
    workflow.write_text("custom workflow\n", encoding="utf-8")
    with pytest.raises(setup_service_module.MaintenanceOperationError, match="changed after review"):
        apply_setup(_setup_args(checkout, config_path, apply=True, authorize_plan=plan["plan_digest"]), tmp_path / "home", adapter_factory=_factory)


def test_setup_rejects_unexpected_tracked_maintenance_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config_path = tmp_path / "reviewed.json"
    config_path.write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(setup_service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(setup_service_module, "_unexpected_tracked_maintenance_files", lambda *_: [".github/workflows/custom-maintenance.yml"])

    with pytest.raises(setup_service_module.MaintenanceOperationError, match="unexpected tracked"):
        plan_setup(_setup_args(checkout, config_path), tmp_path / "home", adapter_factory=_factory)


def test_fleet_setup_fully_pages_and_reports_plans_and_missing_inputs(tmp_path: Path) -> None:
    checkout_root = tmp_path / "checkouts"
    checkout_root.mkdir()
    widget = checkout_root / "widget"
    _checkout_with_origin(widget, "Acme/Widget")
    widget_config = widget / CONFIG_RELATIVE_PATH
    widget_config.parent.mkdir(parents=True)
    widget_config.write_bytes(canonical_config_bytes(_config(101, "Acme/Widget")))
    subprocess.run(["git", "-C", str(widget), "add", "--", CONFIG_RELATIVE_PATH], check=True)
    _checkout_with_origin(checkout_root / "repo1", "Acme/Repo1")

    page_calls: list[int] = []

    class FleetAdapter(FakeAdapter):
        def repository(self) -> dict[str, object]:
            return {"id": 101, "full_name": self.target.full_name, "default_branch": "main"}

        def _request(self, _method: str, endpoint: str, *, operation: str) -> object:
            if endpoint == "users/Acme":
                return {"id": 7, "login": "Acme", "type": "Organization"}
            if endpoint.startswith("orgs/Acme/repos?"):
                page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
                page_calls.append(page)
                if page == 1:
                    return [
                        {"id": index, "full_name": f"Acme/Repo{index}", "private": False, "default_branch": "main"}
                        for index in range(1, 101)
                    ]
                if page == 2:
                    return [{"id": 101, "full_name": "Acme/Widget", "private": False, "default_branch": "main"}]
            raise AssertionError(f"unexpected fleet endpoint for {operation}: {endpoint}")

    def factory(target: RepositoryTarget) -> FleetAdapter:
        return FleetAdapter(target, repository_id=101)

    args = SimpleNamespace(
        hostname="github.com", repository=None, checkout=None, config=None, config_directory=None,
        all_public=True, owner="Acme", checkout_root=str(checkout_root), apply=False, authorize_plan=None,
    )
    first = setup_service_module.setup(args, tmp_path / "home", adapter_factory=factory)
    second = setup_service_module.setup(args, tmp_path / "home", adapter_factory=factory)

    assert page_calls == [1, 2, 1, 2]
    assert first["status"] == "incomplete"
    assert first["repository_count"] == 101
    assert len(first["discovered_repository_ids"]) == 101
    assert first["planned_count"] == 1
    assert first["incomplete_count"] == 100
    assert first["aggregate_digest"] == second["aggregate_digest"]
    by_id = {item["repository_id"]: item for item in first["repositories"]}
    assert by_id[101]["status"] == "planned"
    assert by_id[1]["status"] == "tracked_config_missing"
    assert by_id[2]["status"] == "missing_checkout"
    assert not (widget / WORKFLOW_RELATIVE_PATH).exists()
    assert widget_config.read_bytes() == canonical_config_bytes(_config(101, "Acme/Widget"))


def test_fleet_setup_uses_id_named_external_config_without_checkout_mutation_and_rejects_fleet_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout_root = tmp_path / "checkouts"
    checkout_root.mkdir()
    checkout = checkout_root / "widget"
    _checkout_with_origin(checkout, "Acme/Widget")
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    config_path = config_directory / "123.json"
    config_path.write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(setup_service_module, "discover_public_repositories", lambda *_args, **_kwargs: [PublicRepository(123, "Acme/Widget", "main")])

    args = SimpleNamespace(
        hostname="github.com", repository=None, checkout=None, config=None, config_directory=str(config_directory),
        all_public=True, owner="Acme", checkout_root=str(checkout_root), apply=False, authorize_plan=None,
    )
    _validate_selectors(SimpleNamespace(maintenance_mode="setup", **vars(args)))
    result = setup_service_module.setup(args, tmp_path / "home", adapter_factory=_factory)
    assert result["status"] == "planned"
    assert result["fleet_apply_supported"] is False
    assert result["repositories"][0]["repository_id"] == 123
    assert result["repositories"][0]["plan_digest"]
    assert result["repositories"][0]["checkout_status"] == "available"
    assert result["repositories"][0]["config_status"] == "available"
    assert not (checkout / CONFIG_RELATIVE_PATH).exists()
    assert not (checkout / WORKFLOW_RELATIVE_PATH).exists()

    applying = SimpleNamespace(**{**vars(args), "apply": True})
    with pytest.raises(setup_service_module.MaintenanceOperationError, match="fleet setup apply is not supported"):
        setup_service_module.setup(applying, tmp_path / "home", adapter_factory=_factory)


def test_fleet_setup_rejects_discovery_lookup_id_mismatch_before_child_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout_root = tmp_path / "checkouts"
    checkout = checkout_root / "widget"
    _checkout_with_origin(checkout, "Acme/Widget")
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    (config_directory / "123.json").write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(setup_service_module, "discover_public_repositories", lambda *_args, **_kwargs: [PublicRepository(123, "Acme/Widget", "main")])
    args = SimpleNamespace(
        hostname="github.com", repository=None, checkout=None, config=None, config_directory=str(config_directory),
        all_public=True, owner="Acme", checkout_root=str(checkout_root), apply=False, authorize_plan=None,
    )
    result = setup_service_module.setup(
        args, tmp_path / "home", adapter_factory=lambda target: FakeAdapter(target, repository_id=999),
    )
    row = result["repositories"][0]
    assert result["status"] == "incomplete" and result["planned_count"] == 0
    assert row["repository_id"] == 123
    assert row["status"] == "identity_mismatch" and row["reason"] == "discovery_identity_mismatch"
    assert row["plan_digest"] is None
    assert not list((tmp_path / "home").rglob("plans/*.json"))


class PaginatedIssueAdapter(GhCliAdapter):
    def __init__(self) -> None:
        super().__init__(RepositoryTarget(hostname="github.com", owner="Acme", repository="Widget"))
        self.pages: list[int] = []

    def _request(self, _method: str, endpoint: str, *, operation: str) -> object:
        page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
        self.pages.append(page)
        if "/issues?" in endpoint:
            if page == 1:
                return [
                    {"number": index, "html_url": f"https://github.com/Acme/Widget/issues/{index}", "title": "title-marker", "body": "body-marker"}
                    for index in range(1, 101)
                ]
            return [{"number": 101, "html_url": "https://github.com/Acme/Widget/issues/101", "title": "title-marker", "body": "body-marker"}]
        if "/pulls?" in endpoint:
            return []
        raise AssertionError(f"unexpected paginated endpoint for {operation}")


def test_issue_snapshot_fully_pages_and_emits_only_numbers_and_urls() -> None:
    adapter = PaginatedIssueAdapter()
    result = _issue_snapshot(adapter, "Acme/Widget", limit=5)
    assert result["issues"]["count"] == 101
    assert result["issues"]["truncated"] is True
    assert len(result["issues"]["items"]) == 5
    assert adapter.pages == [1, 2, 1]
    serialized = json.dumps(result)
    assert "title-marker" not in serialized
    assert "body-marker" not in serialized


def test_security_counts_fail_unknown_when_any_scan_is_unavailable() -> None:
    class Counts:
        def dependabot_alert_count(self) -> int:
            return 0

        def code_scanning_alert_count(self) -> int:
            raise GitHubError(403, operation="code_scanning_alerts")

        def secret_scanning_alert_count(self) -> int:
            return 0

    result = _security_advisory_counts(Counts())
    assert result == {"status": "unknown", "counts": {"dependabot": 0, "code_scanning": None, "secret_scanning": 0}}


def test_tracked_input_state_reports_only_count_and_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = b" M .github/localsetup-maintenance.json\x00A  src/private-name.py\x00"
    monkeypatch.setattr(service_module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=raw))

    result = service_module._tracked_input_state(tmp_path)

    assert result == {"status": "dirty", "changed_path_count": 2, "digest": hashlib.sha256(raw).hexdigest()}
    assert "localsetup-maintenance.json" not in json.dumps(result)
    assert "private-name.py" not in json.dumps(result)


def test_push_workflow_success_is_only_partial_ci_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    import ls.core.github_repo.ci_evidence as ci_evidence_module

    monkeypatch.setattr(ci_evidence_module, "check_ci_evidence", lambda *_args, **_kwargs: {
        "ok": True,
        "runs": [{"workflow": "ci.yml", "event": "push"}],
    })

    assert service_module._ci_evidence("Acme/Widget", _SHA, ("ci.yml",)) == {"status": "partial", "ok": False}


def test_verify_reports_workflow_drift_unknown_tool_and_missing_security_as_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    config_path = checkout / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(canonical_config_bytes(_config()))
    workflow_path = checkout / WORKFLOW_RELATIVE_PATH
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("custom\n", encoding="utf-8")
    monkeypatch.setattr(service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(service_module, "_tracked", lambda *_: True)
    monkeypatch.setattr(service_module, "_local_head", lambda *_: _SHA)
    monkeypatch.setattr(service_module, "_signature_status", lambda *_: "cryptographically_valid")
    monkeypatch.setattr(service_module, "_unexpected_tracked_maintenance_files", lambda *_: [])
    monkeypatch.setattr(service_module, "_security_advisory_counts", lambda *_: {"status": "unknown", "counts": {"dependabot": 0, "code_scanning": None, "secret_scanning": 0}})
    monkeypatch.setattr(service_module, "_ci_evidence", lambda *_: {"status": "verified", "ok": True})
    monkeypatch.setattr(service_module.shutil, "which", lambda _name: None)

    result = service_module._verify_one(
        None,
        target=RepositoryTarget(hostname="github.com", owner="Acme", repository="Widget"),
        checkout=checkout,
        adapter_factory=_factory,
    )
    assert result["config"] == "current"
    assert result["workflow"] == "drift"
    assert result["capability_status"] == "unknown"
    assert result["security_advisories"]["status"] == "unknown"
    assert result["handoff_ready"] is False


def test_explicit_validation_strips_tokens_and_never_uses_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured.update(kwargs)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "GH_TOKEN" not in environment
        assert "GITHUB_TOKEN" not in environment
        assert "API_SECRET" not in environment
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("GH_TOKEN", "secret-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-github")
    monkeypatch.setenv("API_SECRET", "secret-api")
    monkeypatch.setattr(service_module.subprocess, "run", fake_run)
    monkeypatch.setattr(service_module.shutil, "which", lambda _name: "/usr/bin/python3")

    result = service_module._run_validators(tmp_path, config)
    assert result["python-stdlib/tests"]["status"] == "passed"
    assert captured["shell"] is False
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


def test_successful_validator_that_changes_tracked_file_cannot_leave_handoff_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = _config()
    config["adapters"]["python-stdlib"][0]["argv"] = ["python3", "-m", "unittest", "discover"]
    config_path = checkout / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(canonical_config_bytes(config))
    workflow_path = checkout / WORKFLOW_RELATIVE_PATH
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_bytes(render_shared_workflow(config))
    (checkout / "tracked.txt").write_text("before\n", encoding="utf-8")
    (checkout / "test_mutates.py").write_text(
        "import unittest\nfrom pathlib import Path\n"
        "class MutationTest(unittest.TestCase):\n"
        "    def test_mutates(self):\n"
        "        Path('tracked.txt').write_text('after\\n')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(checkout), "add", "--", "."], check=True)
    clean = {"status": "clean", "changed_path_count": 0, "digest": hashlib.sha256(b"").hexdigest()}
    real_tracked_state = service_module._tracked_input_state
    reads = 0
    def tracked_state(path: Path) -> dict[str, object]:
        nonlocal reads
        reads += 1
        return clean if reads == 1 else real_tracked_state(path)
    monkeypatch.setattr(service_module, "_tracked_input_state", tracked_state)
    monkeypatch.setattr(service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(service_module, "_local_head", lambda *_: _SHA)
    monkeypatch.setattr(service_module, "_signature_status", lambda *_: "cryptographically_valid")
    monkeypatch.setattr(service_module, "_ci_evidence", lambda *_: {"status": "verified", "ok": True})
    monkeypatch.setattr(service_module, "_tool_capabilities", lambda _config: {"python-stdlib": {"status": "supported"}})
    monkeypatch.setattr(service_module, "_security_advisory_counts", lambda *_: {"status": "complete", "counts": {"dependabot": 0, "code_scanning": 0, "secret_scanning": 0}})
    result = service_module._verify_one(
        None, target=RepositoryTarget(hostname="github.com", owner="Acme", repository="Widget"),
        checkout=checkout, adapter_factory=_factory, validate=True,
    )
    assert result["local_validation"]["python-stdlib/tests"]["status"] == "passed"
    assert (checkout / "tracked.txt").read_text() == "after\n"
    assert result["tracked_input_state"]["status"] == "dirty"
    assert result["validation_input_stability"] == "changed_or_unknown"
    assert result["handoff_ready"] is False


def test_run_packet_is_bounded_and_omits_free_form_issue_and_workflow_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    config_path = checkout / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(canonical_config_bytes(_config()))
    monkeypatch.setattr(run_module, "_verify_one", lambda *_args, **_kwargs: {
        "target": {"repository_id": 123, "full_name": "Acme/Widget"},
        "origin": "current",
        "config": "current",
        "config_tracked": True,
        "workflow": "current",
        "workflow_tracked": True,
        "config_digest": "b" * 64,
        "head": {"local": _SHA, "remote_default": _SHA, "status": "current"},
        "ci_evidence": {"status": "verified", "ok": True},
        "tracked_input_state": {"status": "clean", "changed_path_count": 0, "digest": hashlib.sha256(b"").hexdigest()},
        "handoff_ready": True,
        "unexpected_tracked_maintenance_files": 0,
        "commit_signature": "cryptographically_valid",
        "tool_capabilities": {"python-stdlib": {"status": "supported"}},
        "security_advisories": {"status": "complete", "counts": {"dependabot": 0, "code_scanning": 0, "secret_scanning": 0}},
    })

    class SnapshotAdapter(FakeAdapter):
        def _paginate_result(self, suffix: str, *, operation: str, collection_key: str | None = None) -> dict[str, object]:
            if suffix.startswith("issues?"):
                return {"complete": True, "items": [{"number": 7, "html_url": "https://github.com/Acme/Widget/issues/7", "title": "issue-title-secret", "body": "issue-body-secret"}]}
            if suffix.startswith("pulls?"):
                return {"complete": True, "items": []}
            if suffix == "actions/runs":
                return {"complete": True, "items": [{"id": 9, "status": "completed", "conclusion": "failure", "name": "workflow-title-secret", "html_url": "https://github.com/Acme/Widget/actions/runs/9"}]}
            raise AssertionError(f"unexpected snapshot endpoint for {operation}")

    args = SimpleNamespace(
        hostname="github.com", repository="Acme/Widget", checkout=str(checkout), all_public=False,
        owner=None, checkout_root=None, limit=1, validate=False,
    )
    result = run_module.run(args, tmp_path / "home", adapter_factory=lambda target: SnapshotAdapter(target))
    packet = result["repositories"][0]
    assert packet["handoff_ready"] is True
    assert packet["target"]["repository_id"] == 123
    serialized = json.dumps(packet)
    for private_text in ("issue-title-secret", "issue-body-secret", "workflow-title-secret"):
        assert private_text not in serialized


@pytest.mark.parametrize(
    "verification_update,validate,reason",
    [
        ({"handoff_ready": False}, False, "verification_not_ready"),
        ({"local_validation": {"uv/tests": {"status": "failed"}}}, True, "local_validation_incomplete_or_failed"),
        ({"unexpected_tracked_maintenance_files": 1}, False, "unexpected_tracked_maintenance_files"),
    ],
)
def test_run_never_overrides_verification_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_update: dict[str, object],
    validate: bool,
    reason: str,
) -> None:
    checkout = tmp_path / "checkout"
    config_path = checkout / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(canonical_config_bytes(_config()))
    verified = {
        "target": {"repository_id": 123, "full_name": "Acme/Widget"},
        "origin": "current",
        "config": "current",
        "config_tracked": True,
        "workflow": "current",
        "workflow_tracked": True,
        "config_digest": "b" * 64,
        "head": {"local": _SHA, "remote_default": _SHA, "status": "current"},
        "ci_evidence": {"status": "verified", "ok": True},
        "handoff_ready": True,
        "unexpected_tracked_maintenance_files": 0,
        "commit_signature": "cryptographically_valid",
        "tool_capabilities": {"uv": {"status": "supported"}},
        "security_advisories": {"status": "complete", "counts": {"dependabot": 0, "code_scanning": 0, "secret_scanning": 0}},
    }
    verified.update(verification_update)
    monkeypatch.setattr(run_module, "_verify_one", lambda *_args, **_kwargs: verified)

    class SnapshotAdapter(FakeAdapter):
        def _paginate_result(self, suffix: str, *, operation: str, collection_key: str | None = None) -> dict[str, object]:
            if suffix.startswith("issues?") or suffix.startswith("pulls?"):
                return {"complete": True, "items": []}
            if suffix == "actions/runs":
                return {"complete": True, "items": []}
            raise AssertionError(f"unexpected snapshot endpoint for {operation}")

    args = SimpleNamespace(
        hostname="github.com", repository="Acme/Widget", checkout=str(checkout), all_public=False,
        owner=None, checkout_root=None, limit=1, validate=validate,
    )
    result = run_module.run(args, tmp_path / "home", adapter_factory=lambda target: SnapshotAdapter(target))
    packet = result["repositories"][0]
    assert packet["handoff_ready"] is False
    assert reason in packet["readiness_reasons"]


def test_verify_uses_configured_native_workflow_filename_for_exact_head_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    config = _config()
    config["validation_owners"] = ["ci.yml"]
    config_path = checkout / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(canonical_config_bytes(config))
    workflow_path = checkout / WORKFLOW_RELATIVE_PATH
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_bytes(render_shared_workflow(config))
    monkeypatch.setattr(service_module, "_target_origin_matches", lambda *_: True)
    monkeypatch.setattr(service_module, "_tracked", lambda *_: True)
    monkeypatch.setattr(service_module, "_local_head", lambda *_: _SHA)
    monkeypatch.setattr(service_module, "_signature_status", lambda *_: "cryptographically_valid")
    monkeypatch.setattr(service_module, "_unexpected_tracked_maintenance_files", lambda *_: [])
    monkeypatch.setattr(service_module, "_security_advisory_counts", lambda *_: {"status": "complete", "counts": {"dependabot": 0, "code_scanning": 0, "secret_scanning": 0}})
    monkeypatch.setattr(service_module, "_tool_capabilities", lambda _config: {"uv": {"status": "supported"}})
    monkeypatch.setattr(service_module, "_tracked_input_state", lambda *_: {"status": "dirty", "changed_path_count": 1, "digest": "c" * 64})
    captured: dict[str, object] = {}

    def fake_ci_evidence(repository: str, sha: str, workflows: tuple[str, ...], *, timeout_seconds: int) -> dict[str, object]:
        captured.update(repository=repository, sha=sha, workflows=workflows, timeout_seconds=timeout_seconds)
        return {"ok": True, "runs": [{"workflow": "ci.yml", "event": "pull_request"}]}

    import ls.core.github_repo.ci_evidence as ci_evidence_module
    monkeypatch.setattr(ci_evidence_module, "check_ci_evidence", fake_ci_evidence)
    target = RepositoryTarget(hostname="github.com", owner="Acme", repository="Widget")
    result = service_module._verify_one(None, target=target, checkout=checkout, adapter_factory=_factory)

    assert captured == {"repository": "Acme/Widget", "sha": _SHA, "workflows": ("ci.yml",), "timeout_seconds": 15}
    assert result["ci_evidence"] == {"status": "verified", "ok": True}
    assert result["tracked_input_state"] == {"status": "dirty", "changed_path_count": 1, "digest": "c" * 64}
    assert result["handoff_ready"] is False


def test_verified_public_discovery_retains_pending_ids_when_run_limit_is_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repositories = [
        {"id": index, "full_name": f"Acme/Repo{index}", "private": False, "default_branch": "main"}
        for index in range(1, 4)
    ]

    class DiscoveryAdapter(FakeAdapter):
        def _request(self, _method: str, endpoint: str, *, operation: str) -> object:
            if endpoint == "users/Acme":
                return {"id": 7, "login": "Acme", "type": "Organization"}
            if endpoint.startswith("orgs/Acme/repos?"):
                return repositories if "page=1" in endpoint else []
            raise AssertionError(f"unexpected discovery endpoint for {operation}")

    checkouts = {row["full_name"].casefold(): [tmp_path / f"checkout-{row['id']}"] for row in repositories}
    monkeypatch.setattr(service_module, "discover_checkouts", lambda *_args: checkouts)
    monkeypatch.setattr(run_module, "_run_one", lambda repository, **_kwargs: {
        "schema": 1,
        "kind": "localsetup.github-maintenance-handoff",
        "handoff_ready": True,
        "target": {"repository_id": repository.repository_id, "full_name": repository.full_name},
    })
    args = SimpleNamespace(
        hostname="github.com", repository=None, checkout=None, all_public=True,
        owner="Acme", checkout_root=str(tmp_path), limit=1, validate=False,
    )
    result = run_module.run(args, tmp_path / "home", adapter_factory=lambda target: DiscoveryAdapter(target, repository_id=1))
    assert result["repository_count"] == 3
    assert result["discovered_public_repository_count"] == 3
    assert result["processed_repository_count"] == 1
    assert result["pending_repository_count"] == 2
    assert result["pending_repository_ids"] == [2, 3]
    assert result["handoff_ready"] is False
    assert result["pending_status"] == "pending_limit"

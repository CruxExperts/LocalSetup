"""Failed validation and uncertain source state cannot publish a candidate."""
import json
import subprocess
import sys

import pytest

from ls.core.release_docs import application, integration, runtime
from ls.core import versioning
from ls.core.release_docs import planning, render
from ls.tests.versioning_test_helpers import copy_full_repo, init_git_repo, run


def test_hosted_python_hardening_is_confined_to_setup_tool_root(tmp_path, monkeypatch):
    from pathlib import Path
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/publish.yml").read_text())
    step = next(step for step in workflow["jobs"]["prepare-documentation"]["steps"]
                if step["name"] == "Qualify runner-owned Python permissions")
    script = step["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0]
    executable = tmp_path / "python"
    executable.write_text("fixture")
    executable.chmod(0o777)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("pythonLocation", str(tmp_path))
    exec(compile(script, "runner-permissions", "exec"), {})
    assert executable.stat().st_mode & 0o777 == 0o755
    monkeypatch.setenv("pythonLocation", str(tmp_path / "other"))
    (tmp_path / "other").mkdir()
    with pytest.raises(SystemExit, match="Expected a regular"):
        exec(compile(script, "runner-permissions", "exec"), {})


def test_verified_baseline_precedes_the_only_document_scan(tmp_path, monkeypatch):
    from ls.core.release_docs import cli, github
    monkeypatch.setattr(github, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(github, "published_baseline", lambda *args: {
        "commit": "b" * 40, "tag": "v1.2.3", "version": "1.2.3"})
    calls = []
    def planned(root, **kwargs):
        calls.append(kwargs["base"])
        return {"ok": True, "source_commit": "a" * 40, "target_version": "1.3.0"}
    monkeypatch.setattr(planning, "plan", planned)
    assert cli.main(["--repo-root", str(tmp_path), "plan", "--verify-baseline"]) == 0
    assert calls == ["b" * 40]


def test_validation_failure_stops_before_push(tmp_path, monkeypatch):
    evidence = tmp_path / "candidate.json"
    evidence.write_text(json.dumps({"plan": {"source_commit": "a" * 40}, "candidate": {}}))
    monkeypatch.setattr(integration, "git", lambda root, *args:
                        "a" * 40 + "\trefs/heads/main" if args[0] == "ls-remote" else "a" * 40)
    monkeypatch.setattr(integration, "_signing_identity", lambda root: "A" * 40)
    monkeypatch.setattr(application, "apply_candidate", lambda *args: {"changed_paths": ["README.md"]})
    monkeypatch.setattr(versioning, "publish_preflight", lambda *args, **kwargs: {"ok": True})
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "check" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(integration.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        integration.integrate(tmp_path, evidence, push=True)
    assert not any("push" in args for args in commands)


def test_integration_requires_configured_signed_commits(tmp_path, monkeypatch):
    evidence = tmp_path / "candidate.json"
    evidence.write_text(json.dumps({"plan": {"source_commit": "a" * 40}, "candidate": {}}))
    monkeypatch.setattr(integration, "git", lambda root, *args:
                        "true" if args == ("config", "--get", "commit.gpgsign") else "a" * 40)
    monkeypatch.setattr(application, "apply_candidate", lambda *args: {"changed_paths": []})
    assert integration.integrate(tmp_path, evidence)["head"] == "a" * 40
    monkeypatch.setattr(integration, "git", lambda root, *args: "false" if args == (
        "config", "--get", "commit.gpgsign") else "a" * 40)
    with pytest.raises(ValueError, match="signing key"):
        integration.integrate(tmp_path, evidence)


def test_remote_advance_stops_before_applying(tmp_path, monkeypatch):
    evidence = tmp_path / "candidate.json"
    evidence.write_text(json.dumps({"plan": {"source_commit": "a" * 40}, "candidate": {}}))
    monkeypatch.setattr(integration, "git", lambda root, *args:
                        "b" * 40 + "\trefs/heads/main" if args[0] == "ls-remote" else "a" * 40)
    monkeypatch.setattr(application, "apply_candidate", lambda *args: pytest.fail("stale candidate applied"))
    with pytest.raises(ValueError, match="advanced"):
        integration.integrate(tmp_path, evidence, push=True)


def test_runtime_requires_published_wheel_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(runtime, "published_baseline", lambda *args: {"version": "1.2.3", "tag": "v1.2.3"})
    monkeypatch.setattr(runtime, "gh_json", lambda *args: {"assets": [{"name": "localsetup-1.2.3-py3-none-any.whl"}]})
    with pytest.raises(ValueError, match="trusted GitHub asset digest"):
        runtime.provision(tmp_path, tmp_path.parent / "unused-runtime", tmp_path / "inputs", "/unused/python")
    assert not (tmp_path / "inputs").exists()


def test_runtime_installs_locked_backend_before_source_download(tmp_path, monkeypatch):
    import hashlib
    import io
    import zipfile
    from ls.core.agent import runtime_install

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name in ("sdk-build.lock", "sdk-runtime.lock"):
            archive.writestr("ls/config/" + name, "locked-fixture")
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    assets = tmp_path / "inputs"
    monkeypatch.setenv("GH_TOKEN", "private-fixture")
    monkeypatch.setattr(runtime, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(runtime, "published_baseline", lambda *args: {"version": "1.2.3", "tag": "v1.2.3"})
    monkeypatch.setattr(runtime, "gh_json", lambda *args: {"assets": [{
        "name": "localsetup-1.2.3-py3-none-any.whl", "digest": "sha256:" + digest}]})
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "gh":
            (assets / "localsetup-1.2.3-py3-none-any.whl").write_bytes(data.getvalue())
        else:
            assert "GH_TOKEN" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    from ls.core.release_docs import runtime_candidate
    monkeypatch.setattr(runtime_candidate, "build_candidate", lambda *args: {
        "wheel": assets / "candidate.whl", "wheel_sha256": "b" * 64,
        "source_commit": "a" * 40})
    monkeypatch.setattr(runtime_install, "install", lambda *args, **kwargs: {"status": "installed"})
    assert runtime.provision(tmp_path, tmp_path.parent / "fixture-runtime", assets, "/runner/python")["ok"]
    backend = next(i for i, command in enumerate(commands) if "install" in command)
    download = next(i for i, command in enumerate(commands)
                    if "download" in command and str(assets / "sdk-runtime.lock") in command)
    assert backend < download
    assert "--require-hashes" in commands[backend] and "--no-index" in commands[backend]
    assert "--python" in commands[download]
    assert str(assets / "download-env/bin/python") in commands[download]
    assert "--no-build-isolation" in commands[download]


def test_real_preflight_preserves_planned_next_version(tmp_path, monkeypatch):
    """A docs preparation commit cannot shift the canonical release arithmetic."""
    repo = copy_full_repo(tmp_path)
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    init_git_repo(repo, remote)
    home = tmp_path / "signing-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    run(repo, "gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
        "--quick-generate-key", "Release Fixture <fixture@example.com>", "ed25519", "sign", "0")
    keys = run(repo, "gpg", "--batch", "--with-colons", "--list-secret-keys").stdout
    fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
    run(repo, "git", "config", "user.signingkey", fingerprint)
    run(repo, "git", "config", "commit.gpgsign", "true")
    current = (repo / "VERSION").read_text().strip()
    anchor = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    baseline = f"v{current}"
    run(repo, "git", "tag", baseline)
    (repo / ".localsetup-release.json").write_text(json.dumps({
        "schema_version": 1, "policy": "sequential-logical-slices",
        "anchor": {"commit": anchor, "version": current, "tag": baseline}, "overrides": [],
    }))
    run(repo, "git", "add", ".localsetup-release.json")
    run(repo, "git", "commit", "-qm", "fix: exercise release documentation integration", "--no-verify")
    plan = planning.plan(repo, base=baseline)
    plan["published_baseline"] = {"commit": anchor, "tag": baseline}
    target = plan["target_version"]
    assert target != current
    record = {
        "schema_version": 1, "version": target, "source_commit": plan["source_commit"],
        "baseline_tag": baseline, "summary": "Release documentation integration is checked.",
        "highlights": [{"text": "Checks release preparation.", "evidence": [plan["source_commit"]]}],
        "compatibility": ["No compatibility changes."], "update": ["Use the documented update procedure."],
        "verification": ["Verify the archive with its `.sha256` checksum and `.cdx.json` SBOM sidecars."],
    }
    evidence = repo / ".agents/state/integration-test/candidate.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"plan": plan, "candidate": {}}))

    def accepted_candidate(root, *_args):
        outputs = render.render_outputs(root, record)
        outputs[f"ls/docs/releases/{target}.json"] = json.dumps(record) + "\n"
        for path, content in outputs.items():
            (root / path).write_text(content)
        return {"changed_paths": list(outputs)}

    # The model boundary is separately tested; this test executes the real
    # version/docs preparation and all final validators with the test runtime.
    monkeypatch.setattr(application, "apply_candidate", accepted_candidate)
    original_run = subprocess.run

    def use_test_runtime(args, **kwargs):
        if args[:4] == ["uv", "run", "--frozen", "python"]:
            args = [sys.executable, *args[4:]]
        return original_run(args, **kwargs)

    monkeypatch.setattr(integration.subprocess, "run", use_test_runtime)
    result = integration.integrate(repo, evidence)
    assert result["ok"] and result["changed"]
    assert (repo / "VERSION").read_text().strip() == target
    assert planning.plan(repo, base=baseline)["target_version"] == target
    assert not run(repo, "git", "status", "--porcelain").stdout

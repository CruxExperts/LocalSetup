import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "scripts" / "localsetup_secrets.py"
MAP = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "examples" / "map.yaml"


def run_cli(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(CLI), *args],
        cwd=REPO,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_resolve_fake_redacts_password_by_default() -> None:
    result = run_cli("resolve", "admin@example.com", "--backend", "fake", "--map", str(MAP))
    data = payload(result)
    assert result.returncode == 0
    assert data["data"]["password"] == "<redacted>"
    assert data["data"]["_hint"].startswith("Use --show-sensitive")


def test_ensure_dry_run_and_apply(tmp_path: Path) -> None:
    store = tmp_path / "fake.json"
    dry = run_cli("ensure", "postgres.box03.app1", "--backend", "fake", "--map", str(MAP), "--fake-store", str(store))
    dry_data = payload(dry)
    assert dry.returncode == 0
    assert dry_data["warnings"]
    assert not store.exists()
    applied = run_cli(
        "ensure",
        "postgres.box03.app1",
        "--backend",
        "fake",
        "--map",
        str(MAP),
        "--fake-store",
        str(store),
        "--apply",
    )
    assert applied.returncode == 0
    assert store.exists()


def test_set_from_stdin_requires_apply(tmp_path: Path) -> None:
    store = tmp_path / "fake.json"
    result = run_cli(
        "set",
        "postgres.box03.app1",
        "password",
        "--stdin",
        "--backend",
        "fake",
        "--map",
        str(MAP),
        "--fake-store",
        str(store),
        "--apply",
        input_text="placeholder",
    )
    data = payload(result)
    assert result.returncode == 0
    assert data["data"]["changed"] is True


def test_rotate_and_delete_dry_run() -> None:
    rotated = run_cli("rotate", "postgres.box03.app1", "--backend", "fake", "--map", str(MAP))
    deleted = run_cli("delete", "postgres.box03.app1", "--backend", "fake", "--map", str(MAP))
    assert payload(rotated)["warnings"]
    assert payload(deleted)["warnings"]


def test_render_template_and_tracked_output_refusal(tmp_path: Path) -> None:
    template = tmp_path / "template.txt"
    template.write_text("pw={{secret:postgres.box03.app1:password}}\n", encoding="utf-8")
    default_rendered = run_cli("render-template", str(template), "--backend", "fake", "--map", str(MAP))
    default_payload = payload(default_rendered)
    assert default_payload["data"]["rendered"] == "<redacted>"
    assert "fake-postgres" not in default_rendered.stdout
    rendered = run_cli("render-template", str(template), "--backend", "fake", "--map", str(MAP), "--show-sensitive")
    assert "fake-postgres.box03.app1-password" in payload(rendered)["data"]["rendered"]
    refused = run_cli(
        "render-template",
        str(template),
        "--backend",
        "fake",
        "--map",
        str(MAP),
        "--show-sensitive",
        "--output",
        str(REPO / "README.md"),
    )
    refused_payload = payload(refused)
    assert refused.returncode != 0
    assert refused_payload["errors"][0]["code"] == "tracked_output_refused"
    untracked = run_cli(
        "render-template",
        str(template),
        "--backend",
        "fake",
        "--map",
        str(MAP),
        "--show-sensitive",
        "--output",
        str(REPO / "rendered-secrets.txt"),
    )
    untracked_payload = payload(untracked)
    assert untracked.returncode != 0
    assert untracked_payload["errors"][0]["code"] == "tracked_output_refused"
    ignored = run_cli(
        "render-template",
        str(template),
        "--backend",
        "fake",
        "--map",
        str(MAP),
        "--show-sensitive",
        "--output",
        str(REPO / ".env.rendered"),
    )
    assert ignored.returncode == 0
    assert payload(ignored)["data"]["would_write"] is True


def test_keepassxc_vault_methods_fail_closed_before_binary_invocation(tmp_path: Path) -> None:
    binary = tmp_path / "keepassxc-cli"
    sentinel = tmp_path / "keepassxc-cli-invoked"
    script = "\n".join(
        (
            "#!/usr/bin/env python3",
            "from pathlib import Path",
            f"Path({str(sentinel)!r}).touch()",
            "raise SystemExit(1)",
            "",
        )
    )
    _ = binary.write_text(script, encoding="utf-8")
    _ = binary.chmod(0o755)
    operations = (
        ("list",),
        ("search", "postgres"),
        ("resolve", "postgres.box03.app1"),
        ("ensure", "postgres.box03.app1"),
        ("set", "postgres.box03.app1", "username", "operator"),
        ("rotate", "postgres.box03.app1"),
        ("delete", "postgres.box03.app1"),
    )
    for operation in operations:
        result = run_cli(
            *operation,
            "--backend",
            "keepassxc",
            "--keepassxc-binary",
            str(binary),
            "--map",
            str(MAP),
        )
        data = payload(result)
        assert result.returncode != 0
        assert data["ok"] is False
        assert data["errors"][0]["code"] == "interactive_backend_required"
        assert not sentinel.exists()
        assert "Traceback" not in result.stderr

def test_keepassxc_missing_binary_returns_json_error(tmp_path: Path) -> None:
    result = run_cli(
        "resolve",
        "postgres.box03.app1",
        "--backend",
        "keepassxc",
        "--keepassxc-binary",
        str(tmp_path / "missing-keepassxc-cli"),
        "--map",
        str(MAP),
    )
    data = payload(result)
    assert result.returncode != 0
    assert data["errors"][0]["code"] == "missing_backend"
    assert "Traceback" not in result.stderr


def test_vault_backup_refuses_non_ignored_repo_target(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("backend: fake\ndatabase: README.md\n", encoding="utf-8")
    result = run_cli("vault-backup", "--config", str(config), "--output", str(REPO / "README.md.bak"))
    data = payload(result)
    assert result.returncode != 0
    assert data["errors"][0]["code"] == "tracked_output_refused"

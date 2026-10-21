import json
import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "scripts" / "localsetup_secrets.py"


def run_cli(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["python3", str(CLI), *args],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "ls").mkdir(parents=True)
    return repo


def test_repo_local_config_and_map_are_detected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    secrets = repo / ".localsetup" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "config.yaml").write_text("backend: fake\nmap_path: .localsetup/secrets/map.yaml\n", encoding="utf-8")
    (secrets / "map.yaml").write_text("entries:\n  api:\n    box:\n      app:\n        path: App/API\n", encoding="utf-8")
    payload = run_cli(repo, "config-show")
    assert payload["data"]["config"]["backend"] == "fake"
    assert payload["data"]["map_path"].endswith(".localsetup/secrets/map.yaml")


def test_cli_overrides_env_and_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".localsetup" / "secrets").mkdir(parents=True)
    (repo / ".localsetup" / "secrets" / "config.yaml").write_text("backend: keepassxc\n", encoding="utf-8")
    payload = run_cli(
        repo,
        "config-show",
        "--backend",
        "fake",
        env={"LOCALSETUP_SECRETS_BACKEND": "keepassxc"},
    )
    assert payload["data"]["config"]["backend"] == "fake"


def test_repo_local_config_overrides_global_fallback(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    (home / ".config" / "localsetup" / "secrets").mkdir(parents=True)
    (home / ".config" / "localsetup" / "secrets" / "config.yaml").write_text(
        "backend: keepassxc\nprofile: global\n",
        encoding="utf-8",
    )
    (repo / ".localsetup" / "secrets").mkdir(parents=True)
    (repo / ".localsetup" / "secrets" / "config.yaml").write_text(
        "backend: fake\nprofile: repo\n",
        encoding="utf-8",
    )
    payload = run_cli(repo, "config-show", env={"HOME": str(home)})
    assert payload["data"]["config"]["backend"] == "fake"
    assert payload["data"]["config"]["profile"] == "repo"


def test_single_legacy_repo_map_wins_over_global_default_map(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    home = tmp_path / "home"
    (repo / "secrets").mkdir(parents=True)
    legacy_map = repo / "secrets" / "box-secrets-map.yaml"
    legacy_map.write_text("entries:\n  api:\n    box:\n      app:\n        path: Legacy/API\n", encoding="utf-8")
    (home / ".local" / "share" / "localsetup" / "secrets" / "maps").mkdir(parents=True)
    (home / ".local" / "share" / "localsetup" / "secrets" / "maps" / "default.yaml").write_text(
        "entries:\n  api:\n    global:\n      app:\n        path: Global/API\n",
        encoding="utf-8",
    )
    payload = run_cli(repo, "config-show", env={"HOME": str(home)})
    assert payload["data"]["map_path"] == str(legacy_map)


def test_secret_values_rejected_in_config(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / ".localsetup" / "secrets").mkdir(parents=True)
    (repo / ".localsetup" / "secrets" / "config.yaml").write_text("backend: fake\npassword: nope\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(CLI), "config-show"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "secret_value_in_file"

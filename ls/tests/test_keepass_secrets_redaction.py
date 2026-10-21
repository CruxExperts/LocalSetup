import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "scripts" / "localsetup_secrets.py"
MAP = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "examples" / "map.yaml"

spec = importlib.util.spec_from_file_location("localsetup_secrets", CLI)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_redacts_nested_sensitive_values() -> None:
    payload = module.envelope("x", {"password": "secret", "nested": {"api_token": "token"}})
    assert payload["data"]["password"] == "<redacted>"
    assert payload["data"]["nested"]["api_token"] == "<redacted>"
    assert payload["redactions"] == ["nested.api_token", "password"]


def test_export_env_redacted_by_default() -> None:
    result = subprocess.run(
        [
            "python3",
            str(CLI),
            "export-env",
            "DATABASE_PASSWORD=postgres.box03.app1:password",
            "--backend",
            "fake",
            "--map",
            str(MAP),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["data"]["env"]["DATABASE_PASSWORD"] == "<redacted>"
    assert payload["sensitive"] is False
    assert payload["redactions"] == ["env.DATABASE_PASSWORD"]


def test_redacts_common_secret_key_names() -> None:
    payload = module.envelope(
        "x",
        {
            "api-key": "sk-live-example",
            "api_key": "sk-live-example",
            "private-key-pem": "pem",
            "private_key_pem": "pem",
            "passphrase_hint": "hint",
        },
    )
    assert payload["data"]["api-key"] == "<redacted>"
    assert payload["data"]["api_key"] == "<redacted>"
    assert payload["data"]["private-key-pem"] == "<redacted>"
    assert payload["data"]["private_key_pem"] == "<redacted>"
    assert payload["data"]["passphrase_hint"] == "<redacted>"

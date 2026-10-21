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


def parse(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_help_runs() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    assert "schema-dump" in result.stdout


def test_schema_dump_uses_stable_envelope() -> None:
    result = run_cli("schema-dump", "--schema", "command-result")
    payload = parse(result)
    assert result.returncode == 0
    assert set(payload) == {
        "ok",
        "command",
        "data",
        "warnings",
        "errors",
        "sources",
        "sensitive",
        "redactions",
    }
    assert payload["command"] == "schema-dump"


def test_doctor_reports_json_without_keepass_requirement() -> None:
    result = run_cli("doctor", "--backend", "fake", "--map", str(MAP))
    payload = parse(result)
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["backend"]["backend"] == "fake"


def test_invalid_id_returns_json_error() -> None:
    result = run_cli("resolve", "Bad ID!", "--backend", "fake", "--map", str(MAP))
    payload = parse(result)
    assert result.returncode != 0
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] in {"invalid_alias", "invalid_secret_id"}

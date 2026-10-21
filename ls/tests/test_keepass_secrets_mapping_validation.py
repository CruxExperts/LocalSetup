import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "scripts" / "localsetup_secrets.py"
MAP = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "examples" / "map.yaml"


def test_example_map_validates() -> None:
    result = subprocess.run(
        ["python3", str(CLI), "map-validate", "--map", str(MAP)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["data"]["count"] == 3
    assert "mail.box03.example.admin" in payload["data"]["entries"]
    assert payload["data"]["aliases"]["admin@example.com"] == "mail.box03.example.admin"


def test_map_rejects_secret_values(tmp_path: Path) -> None:
    bad_map = tmp_path / "map.yaml"
    bad_map.write_text("entries:\n  api:\n    box:\n      app:\n        password: nope\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(CLI), "map-validate", "--map", str(bad_map)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "secret_value_in_file"


def test_map_rejects_common_secret_key_names(tmp_path: Path) -> None:
    bad_map = tmp_path / "map.yaml"
    bad_map.write_text(
        "\n".join(
            [
                "entries:",
                "  api:",
                "    box:",
                "      app:",
                "        path: App/API",
                "        meta:",
                "          api-key: sk-live-example",
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["python3", str(CLI), "map-validate", "--map", str(bad_map)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert payload["errors"][0]["code"] == "secret_value_in_file"

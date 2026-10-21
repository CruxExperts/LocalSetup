from pathlib import Path

import pytest

from ls.core.query import adopt_recommendations
from ls.core.selection import has_container_project, recommended_packs_for_target


@pytest.mark.parametrize("name", [
    "Dockerfile", "Dockerfile.dev", "api.Dockerfile", "Containerfile",
    "Containerfile.dev", "api.Containerfile", "compose.yml", "compose.yaml",
    "compose.override.yml", "compose.production.yaml", "docker-compose.yml",
    "docker-compose.yaml", "docker-compose.dev.yml", "docker-compose.dev.yaml",
])
def test_container_signals_agree_without_selecting_engine(tmp_path: Path, name: str):
    (tmp_path / name).write_text("# project\n")
    result = adopt_recommendations(tmp_path)
    assert has_container_project(tmp_path)
    assert result["signals"]["docker"] is True  # API compatibility key
    assert result["recommended_packs"] == recommended_packs_for_target(tmp_path)
    assert "ops" in result["recommended_packs"]
    assert "engine" not in result and "privilege_mode" not in result


@pytest.mark.parametrize("name", ["Dockerfile", "Containerfile", "compose.yaml"])
def test_directories_are_not_container_files(tmp_path: Path, name: str):
    (tmp_path / name).mkdir()
    assert not has_container_project(tmp_path)
    assert "ops" not in recommended_packs_for_target(tmp_path)

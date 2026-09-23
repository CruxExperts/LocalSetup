from __future__ import annotations

import json
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "ls" / "tools" / "context_index.py"
LOCALSETUP = REPO_ROOT / "ls" / "tools" / "localsetup.py"


def run_context(repo: Path, home: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--repo", str(repo), "--home", str(home), *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


def run_context_raw(repo: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo", str(repo), "--home", str(home), *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir(mode=0o700)
    (repo / "README.md").write_text(
        "# LocalSetup Demo\n\nInstall workflow context and vector search notes live here.\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def run_workflow():\n    return 'context index vector workflow'\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("OPENAI_API_KEY=sk-test-secret\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "noise.js").write_text("secret token noise\n", encoding="utf-8")
    return repo, home


def test_context_index_rejects_removed_global_scope(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)

    completed = run_context_raw(repo, home, "inventory", "--scope", "global")

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED_SCOPE"


def test_config_validate_rejects_removed_memory_settings(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)
    config = repo / ".localsetup" / "context-index" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
context_index:
  scopes:
    default_query: [repo, global]
    include_global_memory_by_default: true
    definitions:
      global:
        type: global
        roots: []
      personal:
        type: global
        roots:
          - ~/.codex/memories
  memory:
    track_usage: true
""",
        encoding="utf-8",
    )

    completed = run_context_raw(repo, home, "config", "validate")

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert any("context_index.memory" in issue for issue in payload["issues"])
    assert any("default_query" in issue for issue in payload["issues"])
    assert any("include_global_memory_by_default" in issue for issue in payload["issues"])
    assert any("definitions.global" in issue for issue in payload["issues"])
    assert any("definitions.personal.type" in issue for issue in payload["issues"])
    assert any("definitions.personal.roots" in issue for issue in payload["issues"])


def test_context_index_rejects_custom_scope_outside_repo(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)
    memory_root = home / ".codex" / "memories"
    memory_root.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text("# Removed Surface\n", encoding="utf-8")
    config = repo / ".localsetup" / "context-index" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""
context_index:
  scopes:
    definitions:
      personal:
        type: repo
        roots:
          - {memory_root}
        include: ["**/*.md"]
        exclude: []
        max_file_bytes: 1048576
""",
        encoding="utf-8",
    )

    validate = run_context_raw(repo, home, "config", "validate")
    assert validate.returncode != 0
    validate_payload = json.loads(validate.stdout)
    assert any("definitions.personal.roots" in issue for issue in validate_payload["issues"])

    inventory = run_context_raw(repo, home, "inventory", "--scope", "personal")
    assert inventory.returncode != 0
    payload = json.loads(inventory.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED_SCOPE"

    for args in (("stats", "--scope", "personal"), ("mcp", "config", "--scope", "personal"), ("logs", "status", "--scope", "personal")):
        completed = run_context_raw(repo, home, *args)
        assert completed.returncode != 0
        payload = json.loads(completed.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNSUPPORTED_SCOPE"


def test_context_index_rejects_custom_global_type_for_all_commands(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)
    config = repo / ".localsetup" / "context-index" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
context_index:
  scopes:
    definitions:
      personal:
        type: global
        roots: ["."]
        include: ["**/*.md"]
        exclude: []
        max_file_bytes: 1048576
""",
        encoding="utf-8",
    )

    for args in (("inventory", "--scope", "personal"), ("stats", "--scope", "personal"), ("mcp", "config", "--scope", "personal")):
        completed = run_context_raw(repo, home, *args)
        assert completed.returncode != 0
        payload = json.loads(completed.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNSUPPORTED_SCOPE"


def test_public_skill_index_refresh_filters_memory_entries() -> None:
    module_path = REPO_ROOT / "ls" / "tools" / "refresh_public_skill_index.py"
    spec = importlib.util.spec_from_file_location("refresh_public_skill_index_under_test", module_path)
    assert spec is not None and spec.loader is not None
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)

    assert refresh.is_memory_skill_entry({"name": "agent-memory", "description": "Stores long-term context"})
    assert refresh.is_memory_skill_entry({"name": "context-anchor", "description": "Keeps session continuity"})
    assert not refresh.is_memory_skill_entry({"name": "pdf-tools", "description": "Extracts tables from PDF files"})


def test_context_index_schema_has_native_indexes_for_common_searches(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)

    payload = run_context(repo, home, "doctor")

    assert payload["ok"] is True
    db_path = Path(payload["database"])
    with sqlite3.connect(db_path) as con:
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_sources_context_path",
        "idx_sources_context_freshness",
        "idx_sources_context_priority_status",
        "idx_sources_scope_lookup",
        "idx_chunks_context_line_lookup",
        "idx_vectors_context_profile",
        "idx_vectors_profile_modality",
        "idx_worker_runs_context_status",
    }.issubset(indexes)


def test_inventory_excludes_secret_and_noise_paths_by_default(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)

    default_inventory = run_context(repo, home, "inventory")
    paths = {item["path"] for item in default_inventory["files"]}
    assert "README.md" in paths
    assert "src/app.py" in paths
    assert ".env" not in paths
    assert "node_modules/noise.js" not in paths

    with_excludes = run_context(repo, home, "inventory", "--show-excludes")
    excluded = {item["path"] for item in with_excludes["files"] if item["status"] == "excluded"}
    assert ".env" in excluded
    assert "node_modules/noise.js" in excluded


def test_ingest_search_lookup_freshness_and_rebuild(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)

    pre = run_context(repo, home, "agent-preflight")
    assert pre["vector_available"] is False
    assert pre["worklist"]["extract"] >= 2
    assert "README.md" in pre["recommended_action"] or pre["worklist"]["extract"] >= 1

    ingest = run_context(repo, home, "ingest")
    assert ingest["summary"]["sources"] >= 2
    assert ingest["summary"]["vectors"] >= 2

    stats = run_context(repo, home, "stats")
    assert stats["counts"]["sources"] >= 2
    assert stats["counts"]["vectors"] >= 2
    assert stats["fts"]["chunk_fts"] is True

    search = run_context(repo, home, "search", "vector workflow", "--top-k", "3")
    assert search["ok"] is True
    assert 1 <= len(search["results"]) <= 3
    top = search["results"][0]
    assert {
        "path",
        "line_start",
        "line_end",
        "score",
        "vector_score",
        "lexical_score",
        "chunk_id",
        "stale",
        "indexed_at",
        "source_mtime",
        "git_commit",
    }.issubset(top)

    lookup = run_context(repo, home, "lookup", "--chunk-id", top["chunk_id"])
    assert lookup["chunk"]["line_start"] >= 1
    assert lookup["chunk"]["line_end"] >= lookup["chunk"]["line_start"]

    missing_lookup = run_context_raw(repo, home, "lookup", "--chunk-id", "missing")
    assert missing_lookup.returncode != 0
    missing_lookup_payload = json.loads(missing_lookup.stdout)
    assert missing_lookup_payload["ok"] is False
    assert missing_lookup_payload["error"]["code"] == "NOT_FOUND"

    mcp = run_context(repo, home, "mcp", "config")
    assert mcp["ok"] is True
    assert "context_mcp_server.py" in " ".join(mcp["server"]["args"])

    multi = run_context(repo, home, "freshness", "--scope", "repo,framework")
    assert multi["ok"] is True
    assert [ctx["scope"] for ctx in multi["contexts"]] == ["repo", "framework"]

    vector_plan = run_context(repo, home, "vector-rebuild", "plan")
    assert vector_plan["would_rebuild"]["chunks_to_revector"] >= 2
    vector_apply = run_context(repo, home, "vector-rebuild", "apply", "--plan", vector_plan["plan_id"])
    assert vector_apply["rebuilt_vectors"] >= 2

    (repo / "README.md").write_text(
        "# LocalSetup Demo\n\nInstall workflow context changed after ingest.\n",
        encoding="utf-8",
    )
    stale = run_context(repo, home, "stale-files")
    assert "README.md" in stale["read_direct_paths"]

    plan = run_context(repo, home, "rebuild", "plan")
    assert plan["would_delete"]["sources"] >= 2
    rebuilt = run_context(repo, home, "rebuild", "apply", "--plan", plan["plan_id"])
    assert rebuilt["ok"] is True
    assert rebuilt["ingest"]["summary"]["sources"] >= 2

    (repo / "src" / "app.py").unlink()
    tombstone = run_context(repo, home, "ingest", "--changed-only")
    assert tombstone["summary"]["tombstoned"] >= 1
    prune_plan = run_context(repo, home, "prune", "plan")
    assert prune_plan["would_delete"]["deleted_sources"] >= 1
    prune_apply = run_context(repo, home, "prune", "apply", "--plan", prune_plan["plan_id"])
    assert prune_apply["summary"]["deleted_sources"] >= 1


def test_cli_delegates_context_index(tmp_path: Path) -> None:
    repo, home = make_repo(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(LOCALSETUP),
            "--repo",
            str(REPO_ROOT),
            "--home",
            str(home),
            "--target-directory",
            str(repo),
            "context-index",
            "doctor",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["context"]["scope_slug"] == "repo"

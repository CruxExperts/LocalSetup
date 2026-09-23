from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from ls.core.context_index import memory as memory_module
from ls.core.context_index.models import Runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "ls" / "tools" / "context_index.py"


def _source(ref: str, evidence: str) -> dict[str, str]:
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    return {"type": "document", "ref": ref, "sha256": f"sha256:{digest}"}


def _input(content: str, ref: str, evidence: str) -> str:
    return json.dumps({"content": content, "source": _source(ref, evidence)})


def _raw(
    repo: Path,
    home: Path,
    action: str,
    *args: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--repo",
            str(repo),
            "--home",
            str(home),
            "memory",
            action,
            "--database",
            "global",
            *args,
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _run_index(repo: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo", str(repo), "--home", str(home), *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _ok(repo: Path, home: Path, action: str, *args: str, stdin: str | None = None) -> dict[str, Any]:
    result = _raw(repo, home, action, *args, stdin=stdin)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def _error(repo: Path, home: Path, action: str, *args: str, stdin: str | None = None) -> dict[str, Any]:
    result = _raw(repo, home, action, *args, stdin=stdin)
    assert result.returncode != 0
    return json.loads(result.stdout)["error"]


def _configure_central(home: Path, database_path: Path | None = None) -> None:
    path = home / ".config" / "localsetup" / "context-index" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    storage: dict[str, Any] = {"mode": "central_sqlite"}
    if database_path is not None:
        storage["global_database"] = {"path": str(database_path)}
    path.write_text(json.dumps({"context_index": {"storage": storage}}), encoding="utf-8")


def _central_database(home: Path) -> Path:
    return home / ".local" / "share" / "localsetup" / "context-index" / "context-index.sqlite3"


def _configure_memory_identity(repo: Path, memory_uuid: str) -> None:
    path = repo / ".localsetup" / "context-index" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"context_index": {"identity": {"memory_uuid": memory_uuid}}}),
        encoding="utf-8",
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    repo.mkdir()
    home_a.mkdir(mode=0o700)
    home_b.mkdir(mode=0o700)
    _configure_central(home_a)
    _configure_central(home_b)
    _configure_memory_identity(repo, "75ddf9c0-9f41-4c19-8bb2-d8c1771a719a")
    return repo, home_a, home_b, tmp_path


def test_snapshot_replica_revision_tombstone_and_integrity(tmp_path: Path) -> None:
    repo, writer_home, replica_home, root = _setup(tmp_path)
    content_v1 = "Grounded onboarding note: offline replica preserves provenance." \
        " Context-index memory keeps the source reference and hash."
    input_v1 = _input(content_v1, "notes/onboarding.txt", "approved onboarding evidence v1")
    created = _ok(repo, writer_home, "record", stdin=input_v1)["memory"]
    memory_id = created["memory_id"]
    bundle_v1 = root / "snapshot-v1.json"
    exported_v1 = _ok(repo, writer_home, "export", "--output", str(bundle_v1))
    assert exported_v1["sequence"] == 1
    assert bundle_v1.stat().st_mode & 0o777 == 0o600

    imported_v1 = _ok(repo, replica_home, "import", "--input", str(bundle_v1))
    assert imported_v1["role"] == "replica"
    query = _ok(repo, replica_home, "search", "--mode", "lexical", stdin="onboarding provenance")
    assert query["results"][0]["memory"]["content"] == content_v1
    assert _ok(repo, replica_home, "get", memory_id)["memory"]["content"] == content_v1

    content_v2 = "Updated onboarding fact: replica snapshots carry a revisioned tombstone." \
        " Its verified source remains explicit and searchable."
    input_v2 = _input(content_v2, "notes/onboarding-revised.txt", "approved onboarding evidence v2")
    updated = _ok(
        repo,
        writer_home,
        "update",
        memory_id,
        "--expected-revision",
        "1",
        stdin=input_v2,
    )["memory"]
    assert updated["revision"] == 2
    bundle_v2 = root / "snapshot-v2.json"
    _ok(repo, writer_home, "export", "--output", str(bundle_v2))
    _ok(repo, replica_home, "import", "--input", str(bundle_v2))
    current = _ok(repo, replica_home, "get", memory_id)["memory"]
    assert current["revision"] == 2
    assert current["content"] == content_v2
    search_v2 = _ok(repo, replica_home, "search", "--mode", "lexical", stdin="revisioned tombstone")
    assert search_v2["results"][0]["memory"]["memory_id"] == memory_id
    assert _error(repo, replica_home, "import", "--input", str(bundle_v1))["code"] == "MEMORY_STALE_BUNDLE"

    tampered = root / "tampered.json"
    bundle = json.loads(bundle_v2.read_text(encoding="utf-8"))
    bundle["records"][0]["content"] = "tampered content"
    tampered.write_text(json.dumps(bundle), encoding="utf-8")
    os.chmod(tampered, 0o600)
    assert _error(repo, replica_home, "import", "--input", str(tampered))["code"] == "MEMORY_BUNDLE_INVALID"

    deleted = _ok(repo, writer_home, "delete", memory_id, "--expected-revision", "2")["memory"]
    assert deleted["revision"] == 3 and deleted["deleted"] is True and deleted["content"] is None
    bundle_v3 = root / "snapshot-v3.json"
    _ok(repo, writer_home, "export", "--output", str(bundle_v3))
    _ok(repo, replica_home, "import", "--input", str(bundle_v3))
    tombstone = _ok(repo, replica_home, "get", memory_id)["memory"]
    assert tombstone["revision"] == 3 and tombstone["deleted"] is True
    assert tombstone["content"] is None
    assert _ok(repo, replica_home, "search", "--mode", "lexical", stdin="onboarding")["results"] == []
    assert _error(
        repo,
        replica_home,
        "record",
        stdin=_input("must not write", "notes/source.txt", "replica must stay read-only"),
    )["code"] == "MEMORY_READ_ONLY_REPLICA"


def test_replica_rejects_snapshot_from_competing_writer(tmp_path: Path) -> None:
    repo, writer_a, writer_b, root = _setup(tmp_path)
    replica = root / "replica-home"
    replica.mkdir(mode=0o700)
    record_a = _ok(
        repo,
        writer_a,
        "record",
        stdin=_input("First writer memory", "evidence/a.txt", "writer A source"),
    )
    record_b = _ok(
        repo,
        writer_b,
        "record",
        stdin=_input("Competing writer memory", "evidence/b.txt", "writer B source"),
    )
    assert record_a["memory"]["memory_id"] != record_b["memory"]["memory_id"]
    bundle_a = root / "writer-a.json"
    bundle_b = root / "writer-b.json"
    _ok(repo, writer_a, "export", "--output", str(bundle_a))
    _ok(repo, writer_b, "export", "--output", str(bundle_b))
    _ok(repo, replica, "import", "--input", str(bundle_a))
    assert _error(repo, replica, "import", "--input", str(bundle_b))["code"] == "MEMORY_IMPORT_CONFLICT"


def test_derived_index_reset_does_not_delete_authoritative_memory(tmp_path: Path) -> None:
    repo, home, _, _ = _setup(tmp_path)
    (repo / "README.md").write_text("# Reset boundary\nDerived index fixture.\n", encoding="utf-8")
    ingest = _run_index(repo, home, "ingest")
    assert ingest.returncode == 0, ingest.stderr + ingest.stdout

    content = "Authoritative memory survives a derived context-index reset."
    created = _ok(
        repo,
        home,
        "record",
        stdin=_input(content, "evidence/reset-boundary.txt", "reset boundary source"),
    )["memory"]
    plan = _run_index(repo, home, "reset", "plan", "--scope", "repo")
    assert plan.returncode == 0, plan.stderr + plan.stdout
    plan_id = json.loads(plan.stdout)["plan_id"]
    applied = _run_index(repo, home, "reset", "apply", "--scope", "repo", "--plan", plan_id)
    assert applied.returncode == 0, applied.stderr + applied.stdout
    assert json.loads(applied.stdout)["ok"] is True
    assert _ok(repo, home, "get", created["memory_id"])["memory"]["content"] == content


def test_config_init_persists_distinct_memory_identity_for_same_basename_repositories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo_a = tmp_path / "left" / "same-name"
    repo_b = tmp_path / "right" / "same-name"
    home.mkdir(mode=0o700)
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    _configure_central(home)
    legacy_config = repo_a / ".localsetup" / "context-index" / "config.yaml"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text(
        json.dumps({"context_index": {"identity": {"corpus_slug": "same-name"}}}),
        encoding="utf-8",
    )
    os.chmod(legacy_config, 0o600)

    initialized_a = _run_index(repo_a, home, "config", "init")
    initialized_b = _run_index(repo_b, home, "config", "init")
    assert initialized_a.returncode == initialized_b.returncode == 0
    assert json.loads(initialized_a.stdout)["updated"] is True
    assert json.loads(initialized_a.stdout)["created"] is False
    assert json.loads(initialized_b.stdout)["created"] is True

    content_a = "leftuniquesentinel memory"
    content_b = "rightuniquesentinel memory"
    record_a = _ok(repo_a, home, "record", stdin=_input(content_a, "a.txt", "left evidence"))["memory"]
    record_b = _ok(repo_b, home, "record", stdin=_input(content_b, "b.txt", "right evidence"))["memory"]
    assert record_a["context_key"] != record_b["context_key"]

    initialized_again = _run_index(repo_a, home, "config", "init")
    assert initialized_again.returncode == 0
    assert json.loads(initialized_again.stdout)["created"] is False
    assert _ok(repo_a, home, "get", record_a["memory_id"])["memory"]["context_key"] == record_a["context_key"]
    assert _error(repo_b, home, "get", record_a["memory_id"])["code"] == "MEMORY_NOT_FOUND"
    assert _ok(repo_a, home, "search", "--mode", "lexical", stdin="leftuniquesentinel")["results"]
    assert _ok(repo_b, home, "search", "--mode", "lexical", stdin="leftuniquesentinel")["results"] == []


def test_config_init_rejects_external_symlinks_but_accepts_group_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir(mode=0o700)
    os.chmod(repo, 0o2775)
    config_dir = repo / ".localsetup" / "context-index"
    config_dir.mkdir(parents=True)
    os.chmod(config_dir, 0o2775)
    victim = tmp_path / "external.yaml"
    victim.write_text("must remain unchanged", encoding="utf-8")
    config_path = config_dir / "config.yaml"
    config_path.symlink_to(victim)
    for arguments in ((), ("--force",)):
        result = _run_index(repo, home, "config", "init", *arguments)
        assert result.returncode != 0
        assert json.loads(result.stdout)["error"]["code"] == "CONFIG_PATH_UNSAFE"
        assert victim.read_text(encoding="utf-8") == "must remain unchanged"
    config_path.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    config_dir.rmdir()
    config_dir.symlink_to(outside, target_is_directory=True)
    result = _run_index(repo, home, "config", "init")
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "CONFIG_PATH_UNSAFE"
    assert not (outside / "config.yaml").exists()
    config_dir.unlink()
    config_dir.mkdir()
    result = _run_index(repo, home, "config", "init")
    assert result.returncode == 0, result.stdout
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_memory_commands_require_repository_local_uuid(tmp_path: Path) -> None:
    repo, home, _, _ = _setup(tmp_path)
    (repo / ".localsetup" / "context-index" / "config.yaml").unlink()
    global_config_path = home / ".config" / "localsetup" / "context-index" / "config.yaml"
    global_config = json.loads(global_config_path.read_text(encoding="utf-8"))
    global_config["context_index"]["identity"] = {"memory_uuid": "75ddf9c0-9f41-4c19-8bb2-d8c1771a719a"}
    global_config_path.write_text(json.dumps(global_config), encoding="utf-8")
    result = _error(repo, home, "search", "--mode", "lexical", stdin="not initialized")
    assert result["code"] == "MEMORY_IDENTITY_REQUIRED"
    assert not _central_database(home).exists()


def test_central_database_rejects_symlinks_and_insecure_paths(tmp_path: Path) -> None:
    repo, home, _, root = _setup(tmp_path)
    private = root / "private"
    private.mkdir(mode=0o700)
    victim = root / "outside.sqlite3"
    victim.write_bytes(b"must not be touched")
    linked_database = private / "linked.sqlite3"
    linked_database.symlink_to(victim)
    _configure_central(home, linked_database)
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "DATABASE_PATH_UNSAFE"
    assert victim.read_bytes() == b"must not be touched"

    sidecar_directory = root / "sidecar-private"
    sidecar_directory.mkdir(mode=0o700)
    sidecar_victim = root / "outside-wal"
    sidecar_victim.write_bytes(b"wal target must not be touched")
    sidecar_database = sidecar_directory / "central.sqlite3"
    Path(str(sidecar_database) + "-wal").symlink_to(sidecar_victim)
    _configure_central(home, sidecar_database)
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "DATABASE_PATH_UNSAFE"
    assert not sidecar_database.exists()
    assert sidecar_victim.read_bytes() == b"wal target must not be touched"

    insecure_sidecar = sidecar_directory / "insecure-sidecar.sqlite3-wal"
    insecure_sidecar.write_bytes(b"not a private WAL")
    os.chmod(insecure_sidecar, 0o644)
    _configure_central(home, sidecar_directory / "insecure-sidecar.sqlite3")
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "DATABASE_PATH_UNSAFE"
    assert insecure_sidecar.read_bytes() == b"not a private WAL"

    legacy_database = _central_database(home)
    legacy_directory = legacy_database.parent
    legacy_directory.mkdir(parents=True, exist_ok=True)
    for ancestor in (home / ".local", home / ".local/share", home / ".local/share/localsetup"):
        os.chmod(ancestor, 0o700)
    os.chmod(legacy_directory, 0o755)
    with sqlite3.connect(legacy_database) as con:
        con.execute("CREATE TABLE legacy_receipt(value TEXT)")
        con.execute("INSERT INTO legacy_receipt VALUES ('preserved')")
    os.chmod(legacy_database, 0o600)
    _configure_central(home, legacy_database)
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "MEMORY_NOT_FOUND"
    assert legacy_directory.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(legacy_database) as con:
        assert con.execute("SELECT value FROM legacy_receipt").fetchone()[0] == "preserved"

    shared_parent = root / "shared-parent"
    shared_parent.mkdir(mode=0o755)
    os.chmod(shared_parent, 0o755)
    shared_receipt = shared_parent / "unrelated.txt"
    shared_receipt.write_text("preserve shared directory", encoding="utf-8")
    custom_database = shared_parent / "custom.sqlite3"
    _configure_central(home, custom_database)
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "DATABASE_PATH_UNSAFE"
    assert shared_parent.stat().st_mode & 0o777 == 0o755
    assert shared_receipt.read_text(encoding="utf-8") == "preserve shared directory"
    assert not custom_database.exists()

    private.mkdir(mode=0o700, exist_ok=True)
    insecure_file = private / "insecure.sqlite3"
    insecure_file.write_bytes(b"must remain unchanged")
    os.chmod(insecure_file, 0o644)
    _configure_central(home, insecure_file)
    error = _error(repo, home, "get", str(uuid.uuid4()))
    assert error["code"] == "DATABASE_PATH_UNSAFE"
    assert insecure_file.read_bytes() == b"must remain unchanged"
    assert insecure_file.stat().st_mode & 0o777 == 0o644


def _clone_central_database(source: Path, destination: Path, home: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in destination.parents:
        if directory == home.parent:
            break
        os.chmod(directory, 0o700)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists():
            os.chmod(sidecar, 0o600)


def test_replica_rejects_fork_after_skipped_record_revisions(tmp_path: Path) -> None:
    repo, writer_a, writer_b, root = _setup(tmp_path)
    replica = root / "replica-home"
    replica.mkdir(mode=0o700)
    _configure_central(replica)

    initial = _ok(
        repo,
        writer_a,
        "record",
        stdin=_input("Shared record revision one", "fork/shared.txt", "shared evidence one"),
    )["memory"]
    memory_id = initial["memory_id"]
    _ok(
        repo,
        writer_a,
        "update",
        memory_id,
        "--expected-revision",
        "1",
        stdin=_input("Shared record revision two", "fork/shared-v2.txt", "shared evidence two"),
    )
    _clone_central_database(_central_database(writer_a), _central_database(writer_b), writer_b)

    branch_a = _ok(
        repo,
        writer_a,
        "update",
        memory_id,
        "--expected-revision",
        "2",
        stdin=_input("Writer A revision three", "fork/a-v3.txt", "writer A evidence"),
    )["memory"]
    bundle_a = root / "writer-a-v3.json"
    _ok(repo, writer_a, "export", "--output", str(bundle_a))
    _ok(repo, replica, "import", "--input", str(bundle_a))

    _ok(
        repo,
        writer_b,
        "update",
        memory_id,
        "--expected-revision",
        "2",
        stdin=_input("Writer B revision three", "fork/b-v3.txt", "writer B evidence three"),
    )
    _ok(
        repo,
        writer_b,
        "update",
        memory_id,
        "--expected-revision",
        "3",
        stdin=_input("Writer B revision four", "fork/b-v4.txt", "writer B evidence four"),
    )
    bundle_b = root / "writer-b-v4.json"
    _ok(repo, writer_b, "export", "--output", str(bundle_b))
    assert _error(repo, replica, "import", "--input", str(bundle_b))["code"] == "MEMORY_IMPORT_CONFLICT"
    assert _ok(repo, replica, "get", memory_id)["memory"]["content"] == branch_a["content"]


def test_loopback_embedding_does_not_send_ambient_openai_key(monkeypatch: Any) -> None:
    requests_seen: list[dict[str, str]] = []

    class Response:
        status_code = 200

        def iter_content(self, chunk_size: int):
            return iter([json.dumps({"data": [{"embedding": [3.0, 4.0]}]}).encode("utf-8")])

        def close(self) -> None:
            pass

    class Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def post(self, _endpoint: str, **kwargs: Any) -> Response:
            requests_seen.append(kwargs["headers"])
            return Response()

    monkeypatch.setattr(memory_module.requests, "Session", Session)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    rt = Runtime(
        repo_root=Path("."),
        home=Path("."),
        config={
            "context_index": {
                "embeddings": {
                    "endpoint": "http://127.0.0.1:8080/v1/embeddings",
                    "dimensions": 2,
                }
            }
        },
        context={},
        db_path=Path("."),
        scope="repo",
    )

    assert memory_module._loopback_embedding(rt, "private memory", 2) == [0.6, 0.8]
    assert "Authorization" not in requests_seen[-1]

    monkeypatch.setenv("LOCAL_EMBEDDING_KEY", "explicit-secret")
    rt.config["context_index"]["embeddings"]["api_key_env"] = "LOCAL_EMBEDDING_KEY"
    memory_module._loopback_embedding(rt, "private memory", 2)
    assert requests_seen[-1]["Authorization"] == "Bearer explicit-secret"

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ls.core.herdr_ops.provenance import check_provenance, fetch_latest_metadata


def _write_manifest(root: Path) -> Path:
    upstream = root / "ls" / "skills" / "ls-herdr" / "references" / "upstream"
    upstream.mkdir(parents=True)
    source = upstream / "SKILL.source.md"
    source.write_text("upstream skill", encoding="utf-8")
    schema = upstream / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "protocol": 22,
                "schema_version": 1,
                "properties": {"endpoint_protocol_generation": {"type": "integer"}},
            }
        ),
        encoding="utf-8",
    )
    files = [
        {
            "path": source.name,
            "source_path": "skills/herdr/SKILL.md",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        {
            "path": schema.name,
            "source_kind": "bundled-client-schema",
            "sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
        },
    ]
    manifest = {
        "schema_version": 1,
        "source": {
            "repository": "https://github.com/herdrdev/herdr",
            "ref": "v0.9.5",
            "tag_object": "tag-object",
            "commit": "commit-id",
        },
        "upstream_files": files,
        "release": {
            "version": "0.9.5",
            "protocol": 22,
            "endpoint_generation": 1,
            "assets": {"linux-x86_64": "https://github.com/herdrdev/herdr/releases/download/v0.9.5/herdr"},
            "sha256": {"linux-x86_64": "a" * 64},
        },
        "checked_at": "2026-10-19T00:00:00Z",
        "source_watch": {"ref": "main", "files": [{"source_path": "skills/herdr/SKILL.md", "sha256": files[0]["sha256"]}]},
        "coverage": {"upstream": "test"},
    }
    path = upstream / "provenance.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_check_provenance_validates_local_source_and_schema_pins(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)

    result = check_provenance(tmp_path, manifest_path)

    assert result["source"]["status"] == "ok"
    assert result["schema"]["status"] == "ok"
    assert result["binary"]["status"] == "unknown"
    assert result["server"]["status"] == "unknown"
    assert result["upstream"]["status"] == "not_checked"


def test_check_provenance_reports_source_and_schema_drift(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    (manifest_path.parent / "schema.json").write_text('{"protocol": 1}', encoding="utf-8")

    result = check_provenance(tmp_path, manifest_path)

    assert result["source"]["status"] == "drift"
    assert result["schema"]["status"] == "drift"


def test_check_provenance_network_comparison_is_read_only_and_distinguishes_newer_release(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.10.0", "protocol": 22, "endpoint_generation": 1}),
        source_fetcher=lambda _manifest, _timeout: ("a" * 40, {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()}),
    )

    assert result["upstream"]["status"] == "update_available"
    assert not list(manifest_path.parent.glob("*.tmp"))


def test_check_provenance_reports_network_failures_as_unknown(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)

    def unavailable(_: float) -> tuple[str, dict[str, object]]:
        raise OSError("secret host details must not escape")

    result = check_provenance(tmp_path, manifest_path, fetch_latest=True, latest_fetcher=unavailable,
                              source_fetcher=lambda _manifest, _timeout: (_ for _ in ()).throw(OSError("source unavailable")))

    assert result["upstream"]["status"] == "unknown"
    assert result["upstream"]["findings"] == ["official metadata request failed"]
    assert result["source_watch"]["status"] == "unknown"


def test_source_watch_reports_same_release_source_drift_separately(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    result = check_provenance(
        tmp_path, manifest_path, fetch_latest=True,
        latest_fetcher=lambda _: ("GitHub", {"tag_name": "v0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": "0" * 64}),
    )
    assert result["upstream"]["status"] == "current"
    assert result["source_watch"]["status"] == "drift"


def test_fetch_latest_rejects_non_https_or_unofficial_redirect(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return "https://evil.example/latest.json"

        def read(self, _limit: int) -> bytes:
            return b'{"version":"0.9.5"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    try:
        fetch_latest_metadata(1.0)
    except ValueError as exc:
        assert "metadata request failed" in str(exc) or "approved host" in str(exc)
    else:
        raise AssertionError("unofficial redirect was accepted")

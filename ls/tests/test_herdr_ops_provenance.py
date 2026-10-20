from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest

from ls.core.herdr_ops.provenance import check_provenance, fetch_latest_metadata, fetch_source_hashes


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
            "commit": "c" * 40,
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
        latest_fetcher=lambda _: (
            "herdr.dev/latest.json",
            {"version": "0.10.0", "protocol": 22, "endpoint_generation": 1, "sha256": {"linux-x86_64": "b" * 64}},
        ),
        release_commit_fetcher=lambda _tag, _timeout: "d" * 40,
        source_fetcher=lambda _manifest, _timeout: ("a" * 40, {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()}),
    )

    assert result["upstream"]["status"] == "update_available"
    assert result["upstream"]["evidence"]["status"] == "newer_stable"
    assert result["upstream"]["evidence"]["observed"]["commit"] == "d" * 40
    assert result["source_watch"]["evidence"]["files"][0]["pinned_sha256"] == hashlib.sha256(b"upstream skill").hexdigest()
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
    assert result["source_watch"]["evidence"]["files"][0]["observed_sha256"] == "0" * 64


def test_source_watch_legacy_manifest_falls_back_to_release_revision(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: (
            "b" * 40,
            {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()},
        ),
    )

    evidence = result["source_watch"]["evidence"]
    assert result["source_watch"]["status"] == "current"
    assert evidence["pinned_revision"] == "c" * 40
    assert evidence["pinned_immutable_url"] == f"https://github.com/herdrdev/herdr/commit/{'c' * 40}"


def test_source_watch_revision_is_independent_from_release_pin(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_watch"]["commit"] = "a" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: (
            "b" * 40,
            {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()},
        ),
    )

    source_evidence = result["source_watch"]["evidence"]
    release_evidence = result["upstream"]["evidence"]
    assert source_evidence["pinned_revision"] == "a" * 40
    assert source_evidence["pinned_immutable_url"] == f"https://github.com/herdrdev/herdr/commit/{'a' * 40}"
    assert release_evidence["pinned"]["commit"] == "c" * 40
    assert release_evidence["pinned"]["immutable_url"] == f"https://github.com/herdrdev/herdr/commit/{'c' * 40}"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["source"]["commit"] == "c" * 40


def test_invalid_source_watch_revision_does_not_fall_back_or_claim_current(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_watch"]["commit"] = "not-an-immutable-revision"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: (
            "b" * 40,
            {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()},
        ),
    )

    evidence = result["source_watch"]["evidence"]
    assert result["source_watch"]["status"] == "unknown"
    assert evidence["pinned_revision"] is None
    assert evidence["pinned_immutable_url"] is None
    assert evidence["unknown"] == ["source watch revision pin is invalid"]
    assert result["upstream"]["evidence"]["pinned"]["commit"] == "c" * 40

    drift = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": "0" * 64}),
    )
    assert drift["source_watch"]["status"] == "drift"
    assert drift["source_watch"]["evidence"]["unknown"] == ["source watch revision pin is invalid"]


def test_source_watch_keeps_confirmed_drift_when_another_path_is_unknown(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_watch"]["files"].append({"source_path": "src/api/schema.rs", "sha256": "1" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": "0" * 64}),
    )
    evidence = result["source_watch"]["evidence"]
    assert result["source_watch"]["status"] == "drift"
    assert len(evidence["files"]) == 1
    assert len(evidence["unknown"]) == 1


def test_real_source_fetcher_keeps_valid_hash_when_another_path_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_watch"]["files"].append({"source_path": "src/api/schema.rs", "sha256": "1" * 64})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    revision = "b" * 40
    requested: list[str] = []

    class Response:
        def __init__(self, url: str, payload: bytes) -> None:
            self.url = url
            self.payload = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, _limit: int) -> bytes:
            return self.payload

    def urlopen(request: object, *, timeout: float) -> Response:
        url = request.full_url
        requested.append(url)
        if url.endswith("/commits/main"):
            return Response(url, json.dumps({"sha": revision}).encode())
        if url.endswith("/skills/herdr/SKILL.md"):
            return Response(url, b"changed upstream skill")
        if url.endswith("/src/api/schema.rs"):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        raise AssertionError(f"unexpected source request: {url}")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.9.5"}),
    )

    evidence = result["source_watch"]["evidence"]
    assert result["source_watch"]["status"] == "drift"
    assert [item["source_path"] for item in evidence["files"]] == ["skills/herdr/SKILL.md"]
    assert evidence["unknown"] == ["source observation is unavailable for src/api/schema.rs"]
    assert requested[0].endswith("/commits/main")
    assert len(requested) == 3


def test_invalid_source_manifest_is_rejected_before_any_network_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_watch"]["files"].append({"source_path": "../unsafe.rs", "sha256": "1" * 64})
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: pytest.fail("invalid pins must not fetch"))

    with pytest.raises(ValueError, match="invalid or duplicate path"):
        fetch_source_hashes(manifest, timeout=1.0)


def test_unstable_or_unresolved_release_metadata_cannot_be_confirmed_actionable(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    prerelease = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.10.0-rc.1"}),
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()}),
    )
    assert prerelease["upstream"]["status"] == "update_available"
    assert prerelease["upstream"]["evidence"]["status"] == "unstable_or_unknown"

    unstable_github = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: (
            "GitHub latest release API",
            {"tag_name": "v0.10.0", "draft": False, "prerelease": True},
        ),
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()}),
    )
    assert unstable_github["upstream"]["evidence"]["status"] == "unstable_or_unknown"


def test_newer_stable_release_survives_missing_optional_checksum_metadata(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    result = check_provenance(
        tmp_path,
        manifest_path,
        fetch_latest=True,
        latest_fetcher=lambda _: ("herdr.dev/latest.json", {"version": "0.10.0"}),
        release_commit_fetcher=lambda _tag, _timeout: "d" * 40,
        source_fetcher=lambda _manifest, _timeout: ("b" * 40, {"skills/herdr/SKILL.md": hashlib.sha256(b"upstream skill").hexdigest()}),
    )
    assert result["upstream"]["evidence"]["status"] == "newer_stable"
    assert result["upstream"]["evidence"]["observed"]["checksum_status"] == "unknown"


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

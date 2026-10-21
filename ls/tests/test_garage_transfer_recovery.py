"""Offline adversarial transfer recovery tests independent of dispatch fixtures."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))
from ls_garage import downloads, transfers
from ls_garage.reporting import ToolError


class Stream(io.BytesIO):
    def __init__(self, payload, on_read=lambda: None):
        super().__init__(payload)
        self.on_read = on_read
    def read(self, size=-1):
        self.on_read()
        return super().read(size)


class DownloadClient:
    def __init__(self, stream, length):
        self.stream, self.length = stream, length
    def get_object(self, **kwargs):
        return {"Body": self.stream, "ContentLength": self.length, "VersionId": "opaque/+v"}


def request(tmp_path):
    return {"bucket": "example.bucket", "key": "odd/../+ key", "destination": str(tmp_path / "result")}


def test_concurrent_destination_is_never_clobbered(tmp_path):
    destination = tmp_path / "result"
    stream = Stream(b"new", lambda: destination.write_bytes(b"concurrent"))
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(stream, 3), request(tmp_path))
    assert raised.value.code == "destination_exists"
    assert destination.read_bytes() == b"concurrent"
    assert stream.closed
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result"]


def test_overwrite_preserves_exclusive_backup_and_integrity_failure_preserves_original(tmp_path):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True}
    with pytest.raises(ToolError):
        downloads.download(DownloadClient(Stream(b"short"), 6), args)
    assert destination.read_bytes() == b"original"
    result = downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
    assert destination.read_bytes() == b"replacement"
    assert Path(result["backup"]).read_bytes() == b"original"
    assert result["checksum_verified"] is False
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(Stream(b"other"), 5), args)
    assert raised.value.code == "backup_exists"
    assert destination.read_bytes() == b"replacement"


def test_empty_download_and_symlink_rejection(tmp_path):
    result = downloads.download(DownloadClient(Stream(b""), 0), request(tmp_path))
    assert result["content_length"] == 0
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "result")
    with pytest.raises(ToolError) as raised:
        downloads.download(object(), {**request(tmp_path), "destination": str(alias), "overwrite": True})
    assert raised.value.code == "destination_unsafe"


class Multipart:
    def __init__(self, fail_complete=False):
        self.parts, self.calls, self.fail_complete = {}, [], fail_complete
    def create_multipart_upload(self, **kwargs):
        self.calls.append("create")
        return {"UploadId": "opaque/+upload"}
    def upload_part(self, **kwargs):
        body, chunks = kwargs["Body"], []
        while chunk := body.read(64 * 1024**2):
            assert len(chunk) <= 1024**2
            chunks.append(chunk)
        assert sum(map(len, chunks)) == kwargs["ContentLength"]
        number = kwargs["PartNumber"]
        self.parts[number] = "etag" + str(number)
        self.calls.append("part")
        return {"ETag": self.parts[number]}
    def list_parts(self, **kwargs):
        self.calls.append("list")
        return {"Parts": [{"PartNumber": n, "ETag": tag} for n, tag in self.parts.items()]}
    def complete_multipart_upload(self, **kwargs):
        self.calls.append("complete")
        if self.fail_complete:
            raise ToolError("write_outcome_unknown", "response lost", "uncertain")
        return {"ETag": "multipart-2", "VersionId": "opaque/v"}


def upload_args(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"a" * (5 * 1024**2 + 7))
    return {"bucket": "example.bucket", "key": "odd/../+ key", "source": str(source), "checkpoint": str(tmp_path / "checkpoint")}


def test_streamed_multipart_completion_checkpoint_and_no_replay(tmp_path):
    args = upload_args(tmp_path)
    client = Multipart(fail_complete=True)
    with pytest.raises(ToolError):
        transfers.upload(client, args, "account")
    state = json.loads(Path(args["checkpoint"]).read_text())
    assert state["phase"] == "completing"
    assert len(state["parts"]) == 2
    calls = client.calls[:]
    with pytest.raises(ToolError) as raised:
        transfers.upload(client, args, "account", resume=True)
    assert raised.value.exit_name == "uncertain"
    assert client.calls == calls
    assert Path(args["checkpoint"]).stat().st_mode & 0o777 == 0o600
    assert not Path(args["checkpoint"] + ".lock").exists()


def test_resume_reconciles_exact_remote_parts_before_mutation(tmp_path):
    args = upload_args(tmp_path)
    client = Multipart()
    transfers.upload(client, args, "account")
    state = json.loads(Path(args["checkpoint"]).read_text())
    state["phase"] = "uploading"
    transfers._write(Path(args["checkpoint"]), state)
    client.parts[1] = "concurrently-replaced"
    calls = client.calls[:]
    with pytest.raises(ToolError) as raised:
        transfers.upload(client, args, "account", resume=True)
    assert raised.value.exit_name == "uncertain"
    assert client.calls == calls + ["list"]


def test_checkpoint_lock_refuses_overlapping_writer(tmp_path):
    args = upload_args(tmp_path)
    Path(args["checkpoint"] + ".lock").write_text("owned")
    client = Multipart()
    with pytest.raises(ToolError) as raised:
        transfers.upload(client, args, "account")
    assert raised.value.code == "checkpoint_busy"
    assert client.calls == []


def test_file_range_obeys_zero_read_and_seek_contract():
    body = transfers.FileRange(io.BytesIO(b"prefix-content"), 7, 7)
    assert body.readable() and body.seekable()
    assert body.read(0) == b""
    assert body.tell() == 0
    assert body.read(3) == b"con"
    assert body.seek(-3, 2) == 4
    assert body.read() == b"ent"

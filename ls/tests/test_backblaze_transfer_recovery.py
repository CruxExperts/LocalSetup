"""Offline adversarial transfer recovery tests independent of dispatch fixtures."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze/scripts/lib"))
from ls_backblaze import downloads, transfers, validation
from ls_backblaze.reporting import ToolError


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
    assert "recovery" not in result
    assert result["checksum_verified"] is False
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(Stream(b"other"), 5), args)
    assert raised.value.code == "backup_exists"
    assert destination.read_bytes() == b"replacement"


def test_backup_is_independent_and_detects_source_write_after_copy(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True}
    backup = destination.with_name(destination.name + ".backblaze-backup")
    fd = destination.open("r+b", buffering=0)
    original_link = downloads.os.link
    wrote = False

    def link_then_write(source, target, *link_args, **link_kwargs):
        nonlocal wrote
        original_link(source, target, *link_args, **link_kwargs)
        if Path(target) == backup and not wrote:
            wrote = True
            fd.seek(0)
            fd.write(b"changed!")

    monkeypatch.setattr(downloads.os, "link", link_then_write)
    try:
        with pytest.raises(ToolError) as raised:
            downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
        assert raised.value.code == "destination_changed"
        assert backup.read_bytes() == b"original"
        assert destination.read_bytes() == b"changed!"
    finally:
        fd.close()


def test_recovery_retains_writes_through_open_descriptor_after_displacement(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True, "include_recovery_path": True}
    backup = destination.with_name(destination.name + ".backblaze-backup")
    fd = destination.open("r+b", buffering=0)
    original_link = downloads.os.link

    def link_then_write(source, target, *link_args, **link_kwargs):
        result = original_link(source, target, *link_args, **link_kwargs)
        if Path(target) == destination:
            fd.seek(0)
            fd.write(b"changed!")
        return result

    monkeypatch.setattr(downloads.os, "link", link_then_write)
    try:
        result = downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
    finally:
        fd.close()

    assert destination.read_bytes() == b"replacement"
    assert backup.read_bytes() == b"original"
    assert Path(result["recovery"], "original").read_bytes() == b"changed!"


def test_concurrent_destination_appearance_is_not_overwritten(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True}
    original_link = downloads.os.link
    appeared = False

    def link_with_competitor(source, target, *link_args, **link_kwargs):
        nonlocal appeared
        if Path(target) == destination and not appeared:
            appeared = True
            destination.write_bytes(b"concurrent")
        return original_link(source, target, *link_args, **link_kwargs)

    monkeypatch.setattr(downloads.os, "link", link_with_competitor)
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
    assert raised.value.code == "destination_changed"
    assert destination.read_bytes() == b"concurrent"
    backup = destination.with_name(destination.name + ".backblaze-backup")
    assert backup.read_bytes() == b"original"
    recoveries = list(tmp_path.glob(".backblaze-recovery-*/original"))
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == b"original"
    assert raised.value.reconciliation
    assert str(recoveries[0]) in raised.value.reconciliation


def test_overwrite_does_not_adopt_destination_created_during_transfer(tmp_path):
    destination = tmp_path / "result"
    stream = Stream(b"replacement", lambda: destination.write_bytes(b"concurrent"))
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(stream, 11), {**request(tmp_path), "overwrite": True})
    assert raised.value.code == "destination_changed"
    assert destination.read_bytes() == b"concurrent"
    assert not destination.with_name(destination.name + ".backblaze-backup").exists()


def test_replacement_during_displacement_is_restored_without_losing_either_copy(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True}
    original_rename = downloads.os.rename
    replaced = False

    def rename_after_replacement(source, target, *rename_args, **rename_kwargs):
        nonlocal replaced
        if Path(source) == destination and not replaced:
            replaced = True
            destination.unlink()
            destination.write_bytes(b"concurrent")
        return original_rename(source, target, *rename_args, **rename_kwargs)

    monkeypatch.setattr(downloads.os, "rename", rename_after_replacement)
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
    assert raised.value.code == "destination_changed"
    assert destination.read_bytes() == b"concurrent"
    assert destination.with_name(destination.name + ".backblaze-backup").read_bytes() == b"original"
    recoveries = list(tmp_path.glob(".backblaze-recovery-*/original"))
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == b"concurrent"
    assert raised.value.reconciliation
    assert str(recoveries[0]) in raised.value.reconciliation


def test_directory_open_failure_after_publication_is_uncertain(tmp_path, monkeypatch):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True}
    original_open = downloads.os.open
    parent_opens = 0

    def fail_published_directory_open(path, flags, *open_args, **open_kwargs):
        nonlocal parent_opens
        if Path(path) == tmp_path:
            parent_opens += 1
        if Path(path) == tmp_path and parent_opens == 3:
            raise PermissionError("simulated directory-open failure")
        return original_open(path, flags, *open_args, **open_kwargs)

    monkeypatch.setattr(downloads.os, "open", fail_published_directory_open)
    with pytest.raises(ToolError) as raised:
        downloads.download(DownloadClient(Stream(b"replacement"), 11), args)
    assert raised.value.code == "local_publish_unknown"
    assert raised.value.exit_name == "uncertain"
    assert destination.read_bytes() == b"replacement"
    assert destination.with_name(destination.name + ".backblaze-backup").read_bytes() == b"original"
    recoveries = list(tmp_path.glob(".backblaze-recovery-*/original"))
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == b"original"
    assert raised.value.reconciliation
    assert str(recoveries[0]) in raised.value.reconciliation


def test_valid_long_destination_name_can_be_overwritten(tmp_path):
    destination = tmp_path / ("r" * 240)
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "destination": str(destination), "overwrite": True}

    result = downloads.download(DownloadClient(Stream(b"replacement"), 11), args)

    assert destination.read_bytes() == b"replacement"
    assert Path(result["backup"]).name.startswith(".backblaze-backup-")
    assert Path(result["backup"]).read_bytes() == b"original"
    assert "recovery" not in result


def test_empty_download_and_symlink_rejection(tmp_path):
    result = downloads.download(DownloadClient(Stream(b""), 0), request(tmp_path))
    assert result["content_length"] == 0
    assert "recovery" not in result
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "result")
    with pytest.raises(ToolError) as raised:
        downloads.download(object(), {**request(tmp_path), "destination": str(alias), "overwrite": True})
    assert raised.value.code == "destination_unsafe"


def test_get_object_result_shape_includes_nullable_recovery():
    from ls_backblaze.result_shapes import payload

    shape = payload("s3", "GetObject")
    assert shape["properties"]["recovery"] == {"type": ["string", "null"]}
    assert "recovery" not in shape["required"]


def test_recovery_path_request_flag_is_optional_and_boolean(tmp_path):
    args = {"bucket": "example.bucket", "key": "object", "destination": str(tmp_path / "result")}
    assert validation.validate("s3.GetObject", args)[2] == args
    opted_in = {**args, "overwrite": True, "include_recovery_path": True}
    assert validation.validate("s3.GetObject", opted_in)[2] == opted_in
    with pytest.raises(ToolError):
        validation.validate("s3.GetObject", {**args, "include_recovery_path": "yes"})


def test_opt_in_recovery_path_is_returned_for_overwrite(tmp_path):
    destination = tmp_path / "result"
    destination.write_bytes(b"original")
    args = {**request(tmp_path), "overwrite": True, "include_recovery_path": True}

    result = downloads.download(DownloadClient(Stream(b"replacement"), 11), args)

    recovery = Path(result["recovery"], "original")
    assert recovery.read_bytes() == b"original"


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

"""Bounded directory bundle shipping through the normal signed manifest path."""
from __future__ import annotations
import base64
import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any
from .ship import ship_file_drop

MAX_BUNDLE = 10 * 1024 * 1024

class _BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise ValueError("bundle too large")
        return super().write(data)

def tar_gz_directory(src_dir: Path, *, max_bytes: int = MAX_BUNDLE) -> bytes:
    if max_bytes < 1 or max_bytes > MAX_BUNDLE:
        raise ValueError("bundle limit exceeds safe maximum")
    source = Path(src_dir).resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError("bundle source must be directory")
    buffer = _BoundedBuffer(max_bytes)
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("bundle source contains unsupported entry")
            if path.is_file() and path.stat().st_size > max_bytes:
                raise ValueError("bundle member too large")
            archive.add(path, arcname=str(Path(source.name) / path.relative_to(source)), recursive=False)
    return buffer.getvalue()

def ship_bundle_file_drop(src_dir: Path, registry_path: Path, out_dir: Path, stem: str = "", *,
                          from_agent_id: str, to_agent_ids: list[str], max_bytes: int = MAX_BUNDLE,
                          queue_root: Path | None = None, skip_pre_ship: bool = False,
                          pre_ship_cwd: Path | None = None) -> dict[str, Any]:
    data = tar_gz_directory(src_dir, max_bytes=max_bytes)
    digest = hashlib.sha256(data).hexdigest()
    manifest: dict[str, Any] = {"manifest_version": "1", "from_agent_id": from_agent_id,
        "to_agent_ids": to_agent_ids, "prd_body": f"Bundle SHA-256: {digest}\n",
        "prd_filename": "bundle.prd.md", "attachments": [{"path": "bundle.tar.gz",
        "sha256": digest, "bytes": len(data), "content_b64": base64.b64encode(data).decode("ascii")}]}
    return ship_file_drop(manifest, registry_path, out_dir, stem, queue_root=queue_root,
                          skip_pre_ship=skip_pre_ship, pre_ship_cwd=pre_ship_cwd)

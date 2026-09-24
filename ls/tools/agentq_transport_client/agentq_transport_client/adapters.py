"""Transport adapters carrying only opaque bounded binary Agent Q objects."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Iterator, Protocol
from .crypto_pipeline import read_bounded
from .file_drop import iter_candidates
from .ship import SUFFIX, _write_blob

class TransportAdapter(Protocol):
    def pull_new(self) -> Iterator[tuple[bytes, dict[str, Any]]]: ...
    def push(self, blob_bytes: bytes, metadata: dict[str, Any]) -> str: ...

def file_drop_pull_iter(roots: list[Path], sealed_extension: str = SUFFIX) -> Iterator[tuple[bytes, dict[str, Any]]]:
    if sealed_extension != SUFFIX:
        raise ValueError("MIGRATION_REQUIRED")
    for sealed, ready in iter_candidates(roots, SUFFIX):
        yield read_bounded(sealed), {"path": str(sealed), "ready": str(ready), "transport": "file_drop"}

class FileDropAdapter:
    def __init__(self, roots: list[Path], out_dir: Path, sealed_extension: str = SUFFIX):
        if sealed_extension != SUFFIX:
            raise ValueError("MIGRATION_REQUIRED")
        self.roots = [Path(root) for root in roots]
        self.out_dir = Path(out_dir)
    def pull_new(self) -> Iterator[tuple[bytes, dict[str, Any]]]:
        return file_drop_pull_iter(self.roots)
    def push(self, blob_bytes: bytes, metadata: dict[str, Any]) -> str:
        if not isinstance(blob_bytes, bytes):
            raise ValueError("binary envelope required")
        return _write_blob(blob_bytes, self.out_dir)["sealed"]

class StubDriveAdapter(FileDropAdapter):
    pass

class StubTelegramAdapter:
    def pull_new(self) -> Iterator[tuple[bytes, dict[str, Any]]]:
        return iter(())
    def push(self, blob_bytes: bytes, metadata: dict[str, Any]) -> str:
        raise NotImplementedError("Telegram adapter unavailable")

class MailAdapterStub(StubTelegramAdapter):
    pass

ADAPTER_REGISTRY = {"file_drop": FileDropAdapter, "drive_sync": StubDriveAdapter,
                    "dropbox_sync": StubDriveAdapter, "telegram": StubTelegramAdapter, "mail": MailAdapterStub}

def get_adapter(name: str, **kwargs: Any) -> Any:
    try:
        return ADAPTER_REGISTRY[name](**kwargs)
    except KeyError as exc:
        raise ValueError("unknown adapter") from exc

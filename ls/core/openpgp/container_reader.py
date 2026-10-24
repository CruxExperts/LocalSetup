"""Verified file-backed reads beneath an ordinary authorized file interface.

Callers configure admission and carrier sources at the trusted runtime boundary.
Requests select only a file reference; they never supply cryptographic authority.
"""
from __future__ import annotations

import codecs
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import hashlib
from itertools import chain
import os
from pathlib import Path
import tempfile
import time
from typing import Callable

from . import artifacts
from .artifact_records import canonical_bytes, validate_opaque_id
from .container_preparation import ContainerPreparationLimits, _Budget
from .container_delta_codec import DeltaCodec
from .container_reconstruction import ReconstructionError, extract_container_file
from .container_sources import LocalFileRangeSource, SourceChangedError
from .envelope_io import AnchoredFile
from .secrets import SecretReference, SecretResolver


class ContainerReadError(PermissionError):
    """A bounded read state without private paths or cryptographic diagnostics."""

    def __init__(self, code: str):
        self.code = code if code in {
            "unauthorized", "unavailable", "incomplete", "conflict", "expired",
            "changed", "limit_exceeded", "file_missing", "pending", "unsupported",
        } else "unavailable"
        super().__init__("Protected file read: " + self.code)


@dataclass(frozen=True, slots=True, repr=False)
class ContainerReader:
    """Operator-bound reader; source paths identify carriers, never authority.

    The runtime supplies the approved source inventory and optional live binding
    check. Reading does not create keys, enroll peers or add carrier sources.
    """

    policy: artifacts.ArtifactPolicy | artifacts.ArtifactDiscoveryPolicy
    trust_state_path: Path
    gnupg_home: Path
    passphrase_reference: SecretReference | None = None
    secret_resolver: SecretResolver | None = field(default=None, repr=False)
    source_paths: tuple[Path, ...] = ()
    limits: ContainerPreparationLimits = field(default_factory=ContainerPreparationLimits)
    max_sources: int = 128
    cache_seconds: float = 30
    binding_check: Callable[[], str | None] | None = field(default=None, repr=False, compare=False)
    source_resolver: Callable[[Callable[[], None]], tuple[Path, ...]] | None = field(default=None, repr=False, compare=False)
    metadata_resolver: Callable | None = field(default=None, repr=False, compare=False)
    reference_resolver: Callable | None = field(default=None, repr=False, compare=False)
    delta_codec: DeltaCodec | None = None
    max_delta_depth: int = 16
    dependency_check: Callable | None = field(default=None, repr=False, compare=False)
    after_read: Callable | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        artifacts._require_policy(self.policy)
        if (not isinstance(self.limits, ContainerPreparationLimits)
                or type(self.max_sources) is not int or not 1 <= self.max_sources <= 1024
                or type(self.cache_seconds) not in (int, float) or not 0 < self.cache_seconds <= 300
                or type(self.source_paths) is not tuple or len(self.source_paths) > self.max_sources
                or len(set(self.source_paths)) != len(self.source_paths)
                or (self.passphrase_reference is None) != (self.secret_resolver is None)
                or (self.passphrase_reference is not None and not isinstance(self.passphrase_reference, SecretReference))
                or (self.secret_resolver is not None and not isinstance(self.secret_resolver, SecretResolver))
                or (self.binding_check is not None and not callable(self.binding_check))
                or (self.source_resolver is not None and not callable(self.source_resolver))
                or (self.metadata_resolver is not None and not callable(self.metadata_resolver))
                or (self.reference_resolver is not None and not callable(self.reference_resolver))):
            raise ValueError("Invalid protected read configuration")
        if (self.delta_codec is not None and not isinstance(self.delta_codec, DeltaCodec)
                or type(self.max_delta_depth) is not int or not 1 <= self.max_delta_depth <= 64
                or self.dependency_check is not None and not callable(self.dependency_check)
                or self.after_read is not None and not callable(self.after_read)):
            raise ValueError("Invalid protected delta configuration")
        for path in (self.trust_state_path, self.gnupg_home, *self.source_paths):
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError("Protected read paths must be explicit and absolute")

    def check(self, metadata=None):
        binding = self.binding_check() if self.binding_check is not None else None
        if binding is not None and (type(binding) is not str or len(binding) != 64
                or any(character not in "0123456789abcdef" for character in binding)):
            raise ContainerReadError("unauthorized")
        artifacts._current_authorities(self.trust_state_path, self.policy,
                                       include_controller=self.policy.controller_fingerprint is not None)
        artifacts._require_fresh_policy(self.policy)
        if metadata is not None:
            artifacts._policy_logical(metadata.logical, self.policy, historical=False,
                                      limits=self.limits.container_limits)
        return binding

    def prepare(self, path: Path, reference: str, *, file_id: str | None,
                expected_stat: os.stat_result, check: Callable[[], None], deadline: float,
                expected_commitment: str | None = None, _budget=None):
        """Authenticate the requested carrier, then stage one complete file or index."""
        stack = ExitStack()
        try:
            check()
            binding = self.check()
            if file_id is not None:
                validate_opaque_id(file_id)
            remaining = min(self.limits.timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                raise ContainerReadError("expired")

            def cancelled():
                check()
                return self.limits.cancel_check is not None and self.limits.cancel_check()

            limits = replace(self.limits, timeout_seconds=remaining, cancel_check=cancelled)
            budget = _budget if _budget is not None else _Budget(limits)
            if _budget is not None:
                limits = budget.limits
                budget.checkpoint()
            boundary = artifacts._require_boundary(self.policy.boundary)
            workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".lssc-read-", dir=boundary)))
            policy = replace(self.policy, boundary=workspace)
            first_source = stack.enter_context(LocalFileRangeSource(path, expected_stat=expected_stat))
            metadata = self._inspect(first_source, policy, budget, workspace)
            if expected_commitment is not None and metadata.logical_commitment_sha256 != expected_commitment:
                raise ContainerReadError("changed")
            effective = self
            if self.metadata_resolver is not None:
                effective = self.metadata_resolver(metadata, budget)
                if (not isinstance(effective, ContainerReader) or effective.policy != self.policy
                        or effective.trust_state_path != self.trust_state_path
                        or effective.gnupg_home != self.gnupg_home):
                    raise ContainerReadError("unauthorized")
                if self.check(metadata) != binding:
                    raise ContainerReadError("changed")
                binding = effective.check(metadata)
            sources = {metadata.key: first_source}
            selected = file_id
            if selected is None and len(metadata.logical["files"]) == 1:
                selected = metadata.logical["files"][0]["file_id"]
            output = workspace / "content"
            delta_context, dependencies = None, []
            if selected is None:
                artifacts._accept_verified_metadata(metadata, policy=policy, trust_state_path=self.trust_state_path)
                expected_digest, expected_size = self._index(output, metadata, reference, budget)
            else:
                if not any(item["file_id"] == selected for item in metadata.logical["files"]):
                    raise ContainerReadError("file_missing")
                # Freeze discovery once for the whole chain. Later arrivals are
                # considered by the next request, never spliced into this one.
                effective = replace(effective, source_paths=effective._paths(budget), source_resolver=None)
                effective._sources(sources, stack, metadata, policy, budget, workspace, path, selected)
                if effective.delta_codec is not None:
                    from .container_delta_reader import delta_read_context
                    delta_context = delta_read_context(effective, stack, policy, budget, workspace, dependencies)
                receipt = extract_container_file(sources, selected, output.name, authorized=True,
                    policy=policy, trust_state_path=self.trust_state_path, gnupg_home=self.gnupg_home,
                    passphrase_reference=self.passphrase_reference, secret_resolver=self.secret_resolver,
                    limits=limits, _budget=budget, _delta_context=delta_context)
                expected_digest, expected_size = receipt.plaintext_sha256, receipt.plaintext_length
            opened = stack.enter_context(AnchoredFile(output, private=True))
            digest, text, size = _inspect_output(opened, budget)
            if (digest, size) != (expected_digest, expected_size):
                raise ContainerReadError("changed")
            if effective.check(metadata) != binding:
                raise ContainerReadError("changed")
            check()
            if selected is not None and effective.after_read is not None:
                effective.after_read(effective, path, metadata, budget, check)
                budget.checkpoint()
                if effective.check(metadata) != binding:
                    raise ContainerReadError('changed')
            result = VerifiedContainerRead(stack, effective, metadata, opened,
                (*tuple((source, source.generation) for source in sources.values()), *dependencies),
                digest, size, text, min(deadline, time.monotonic() + self.cache_seconds), binding,
                dependencies=tuple(delta_context.metadata) if delta_context is not None else ())
            result.check(check)
            return result
        except BaseException as error:
            stack.close()
            if isinstance(error, (KeyboardInterrupt, SystemExit, ContainerReadError)):
                raise
            code = "unauthorized" if isinstance(error, artifacts.ArtifactError) else "unavailable"
            if isinstance(error, ReconstructionError):
                code = {"unsupported_delta": "unsupported", "conflicting_volumes": "conflict", "duplicate_volume": "conflict",
                        "base_mismatch": "conflict", "delta_cycle": "conflict", "delta_depth": "limit_exceeded",
                        "source_changed": "changed",
                        "incomplete": "incomplete", "file_missing": "file_missing",
                        "file_limit": "limit_exceeded", "resource_limit": "limit_exceeded"}.get(error.code, "unavailable")
        raise ContainerReadError(code)

    def _inspect(self, source, policy, budget, workspace):
        budget.checkpoint()
        return artifacts.inspect_container_metadata(source, policy=policy,
            trust_state_path=self.trust_state_path, gnupg_home=self.gnupg_home,
            passphrase_reference=self.passphrase_reference, secret_resolver=self.secret_resolver,
            limits=budget.limits.container_limits, _budget=budget, _workspace=workspace)

    def _paths(self, budget):
        paths = self.source_paths if self.source_resolver is None else self.source_resolver(budget.checkpoint)
        if (type(paths) is not tuple or len(paths) > self.max_sources or len(set(paths)) != len(paths)
                or any(not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts for path in paths)):
            raise ContainerReadError("limit_exceeded")
        return paths

    def _sources(self, sources, stack, metadata, policy, budget, workspace, requested, file_id):
        required = {volume["volume_id"] for volume in metadata.inventory["volumes"]
                    if file_id is None or any(item["file_id"] == file_id for item in volume["messages"])}
        found = {metadata.origin_volume_id}
        paths = self._paths(budget)
        for path in sorted(paths):
            if required <= found:
                break
            budget.checkpoint()
            if path == requested:
                continue
            if len(sources) >= self.max_sources:
                raise ContainerReadError("limit_exceeded")
            # Only operator-selected carriers are opened; failures never authorize
            # searching adjacent directories or adding sources from a manifest.
            try:
                source = LocalFileRangeSource(path)
                stack.callback(source.close)
                candidate = self._inspect(source, policy, budget, workspace)
            except (OSError, artifacts.ArtifactError):
                budget.checkpoint()
                continue
            if candidate.logical_commitment_sha256 != metadata.logical_commitment_sha256:
                source.close()
                continue
            if candidate.inventory != metadata.inventory:
                raise ContainerReadError("conflict")
            if candidate.origin_volume_id in found or candidate.origin_volume_id not in required:
                source.close()
                continue
            sources[candidate.key] = source
            found.add(candidate.origin_volume_id)
        if not required <= found:
            raise ContainerReadError("incomplete")

    @staticmethod
    def _index(output, metadata, reference, budget):
        total, digest = 0, hashlib.sha256()
        with output.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            header = {"kind": "files", "artifact": metadata.logical["artifact_id"],
                      "version": metadata.logical["version_id"], "metadata_verified": True,
                      "payload_verified": False}
            entries = ({"path": reference + "/@files/" + item["file_id"],
                        "name": item["provenance"]["original_name"],
                        "bytes": item["length"], "sha256": item["sha256"]}
                       for item in metadata.logical["files"])
            for item in chain((header,), entries):
                budget.checkpoint()
                data = canonical_bytes(item) + b"\n"
                total += len(data)
                if total > budget.limits.file_limits.max_plaintext_bytes:
                    raise ContainerReadError("limit_exceeded")
                budget.reserve_temp(len(data))
                stream.write(data)
                digest.update(data)
        return digest.hexdigest(), total


def _inspect_output(opened, budget):
    digest, decoder = hashlib.sha256(), codecs.getincrementaldecoder("utf-8")("strict")
    size, text = 0, True
    while True:
        budget.checkpoint()
        data = os.pread(opened.file, 65536, size)
        if not data:
            break
        size += len(data)
        digest.update(data)
        if text:
            try:
                decoder.decode(data)
                text = not any(value == 127 or value < 32 and value not in (9, 10, 12, 13) for value in data)
            except UnicodeDecodeError:
                text = False
    if text:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            text = False
    if not opened.unchanged():
        raise ContainerReadError("changed")
    return digest.hexdigest(), text, size


@dataclass(slots=True, repr=False)
class VerifiedContainerRead:
    stack: ExitStack
    reader: ContainerReader
    metadata: artifacts.VerifiedContainerMetadata | None
    opened: AnchoredFile
    sources: tuple
    digest: str
    size: int
    text: bool
    expires: float
    binding: str | None
    closed: bool = False
    dependencies: tuple = ()

    def check(self, grant_check):
        grant_check()
        if self.closed or time.monotonic() >= self.expires:
            raise ContainerReadError("expired")
        try:
            if self.reader.check(self.metadata) != self.binding:
                raise ContainerReadError("changed")
            for metadata in self.dependencies:
                artifacts._policy_logical(metadata.logical, self.reader.policy, historical=False,
                                          limits=self.reader.limits.container_limits)
            if not self.opened.unchanged() or any(source.generation != generation for source, generation in self.sources):
                raise ContainerReadError("changed")
        except ContainerReadError:
            raise
        except SourceChangedError:
            raise ContainerReadError("changed") from None
        except Exception:
            raise ContainerReadError("unauthorized") from None
        grant_check()

    def read(self, offset: int, count: int, grant_check) -> bytes:
        self.check(grant_check)
        if type(offset) is not int or type(count) is not int or offset < 0 or not 0 <= count <= 8192:
            raise ValueError("Invalid protected page range")
        data = os.pread(self.opened.file, count, offset)
        self.check(grant_check)
        return data

    def close(self):
        if not self.closed:
            self.closed = True
            self.stack.close()

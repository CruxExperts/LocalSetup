from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import yaml

from ls.core.openpgp.container_format import (
    CONTAINER_FORMAT,
    ContainerFormatError,
    ContainerLimitError,
    ContainerLimits,
    ContainerWriteResult,
    MessageDescriptorError,
    UnsupportedContainerVersion,
    copy_message,
    read_metadata,
    scan_message_descriptors,
    write_container,
)
from ls.core.openpgp.container_schema import (
    PROTECTED_METADATA_SCHEMA,
    ProtectedSchemaError,
    UnsupportedProtectedSchemaVersion,
    UnsupportedRequiredFeature,
    validate_protected_schema,
)
from ls.core.openpgp.container_sources import (
    BytesRangeSource,
    ContainerSourceError,
    LocalFileRangeSource,
    SourceChangedError,
)


RECIPIENT_A = "A" * 40
RECIPIENT_B = "B" * 40


def _container(
    *,
    metadata: bytes = b"encrypted metadata bytes",
    messages: tuple[bytes, ...] = (b"first independently encrypted file",),
    version: int = 1,
) -> tuple[bytes, ContainerWriteResult]:
    output = io.BytesIO()
    result = write_container(
        output,
        recipient_hints=(RECIPIENT_B, RECIPIENT_A) if version == 1 else (),
        metadata_ciphertext=metadata,
        messages=messages,
        version=version,
    )
    return output.getvalue(), result


class _SpySource:
    def __init__(self, data: bytes, *, advertised_size: int | None = None) -> None:
        self.data = data
        self._size = len(data) if advertised_size is None else advertised_size
        self.calls: list[tuple[int, int]] = []

    @property
    def size(self) -> int:
        return self._size

    def read_range(self, offset: int, length: int) -> bytes:
        self.calls.append((offset, length))
        if offset < 0 or length < 0 or offset + length > len(self.data):
            raise ContainerSourceError("range is unavailable in spy source")
        return self.data[offset : offset + length]


def test_writer_emits_deterministic_yaml_and_streaming_message_descriptors() -> None:
    first, result = _container(messages=(b"alpha", b"bravo" * 31))
    second, again = _container(messages=(b"alpha", b"bravo" * 31))
    assert first == second
    assert result == again

    parsed = yaml.safe_load(first)
    assert parsed == {
        "format": CONTAINER_FORMAT,
        "version": 1,
        "recipient_hints": [RECIPIENT_A, RECIPIENT_B],
        "metadata_length": len(b"encrypted metadata bytes"),
        "metadata": b"encrypted metadata bytes",
        "messages": [b"alpha", b"bravo" * 31],
    }
    assert first.index(b"metadata: !!binary |\n") < first.index(b"messages:\n")
    assert b"filename" not in first.lower()
    assert b"provenance" not in first.lower()
    scanned = scan_message_descriptors(BytesRangeSource(first))
    assert scanned == result.message_descriptors
    assert [item.message_index for item in scanned] == [0, 1]
    assert all(item.serialized_length > item.ciphertext_length for item in scanned)


def test_legacy_writer_preserves_exact_v1_bytes() -> None:
    output = io.BytesIO()
    write_container(output, recipient_hints=(RECIPIENT_A,),
                    metadata_ciphertext=b"m", messages=(b"p",))
    assert output.getvalue() == (
        b"format: localsetup.secure-container\nversion: 1\nrecipient_hints:\n"
        b"  - 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n"
        b"metadata_length: 1\nmetadata: !!binary |\n  bQ==\n"
        b"messages:\n  - !!binary |\n    cA==\n"
    )


def test_v2_framing_omits_recipient_identities_and_preserves_descriptors() -> None:
    data, written = _container(version=2, messages=(b"alpha", b"bravo"))
    parsed = yaml.safe_load(data)
    assert parsed == {
        "format": CONTAINER_FORMAT, "version": 2,
        "metadata_length": len(b"encrypted metadata bytes"),
        "metadata": b"encrypted metadata bytes", "messages": [b"alpha", b"bravo"],
    }
    header = read_metadata(BytesRangeSource(data))
    assert header.version == 2 and header.recipient_hints == ()
    assert header.public_header_sha256 == written.public_header_sha256
    assert scan_message_descriptors(BytesRangeSource(data)) == written.message_descriptors
    for field in (b"recipient_hints", RECIPIENT_A.encode(), RECIPIENT_B.encode()):
        assert field not in data


@pytest.mark.parametrize("hints", [(RECIPIENT_A,), (None,), None])
def test_v2_writer_rejects_hints_before_writing(hints) -> None:
    output = io.BytesIO()
    with pytest.raises(ContainerFormatError):
        write_container(output, version=2, recipient_hints=hints,
                        metadata_ciphertext=b"m", messages=(b"p",))
    assert output.getvalue() == b""


def test_versioned_headers_reject_recipient_field_substitution() -> None:
    private, _ = _container(version=2)
    legacy, _ = _container()
    for value in (
        private.replace(b"metadata_length:", b"recipient_hints: []\nmetadata_length:", 1),
        private.replace(b"version: 2\n", b"version: 1\nrecipient_hints:\n", 1),
        legacy.replace(b"version: 1\n", b"version: 2\n", 1),
    ):
        with pytest.raises(ContainerFormatError):
            read_metadata(BytesRangeSource(value))


@pytest.mark.parametrize("version", [1, 2])
def test_metadata_read_stops_before_message_bytes_for_large_unavailable_tail(version: int) -> None:
    data, _ = _container(messages=(b"bulk" * 100,), version=version)
    source = BytesRangeSource(data)
    parsed_header = read_metadata(source)
    body_start = parsed_header.messages_offset + len(b"messages:\n")

    spy = _SpySource(data[:body_start], advertised_size=10**12)
    result = read_metadata(spy)
    assert result.encrypted_metadata == b"encrypted metadata bytes"
    assert result.messages_offset == parsed_header.messages_offset
    assert spy.calls
    assert all(offset + length <= body_start for offset, length in spy.calls)


@pytest.mark.parametrize("version", [1, 2])
def test_selected_message_reads_only_its_range_and_checks_authenticated_expectation(version: int) -> None:
    data, written = _container(messages=(b"first" * 40, b"middle" * 45, b"selected" * 50), version=version)
    header = read_metadata(BytesRangeSource(data))
    selected = written.message_descriptors[2]
    start = header.messages_offset + selected.offset
    end = start + selected.serialized_length
    spy = _SpySource(data)
    destination = io.BytesIO()

    copied = copy_message(spy, header, selected, destination, expected_index=2)

    assert copied == len(b"selected" * 50)
    assert destination.getvalue() == b"selected" * 50
    selected_range = (start, end)
    first_range = (
        header.messages_offset + written.message_descriptors[0].offset,
        header.messages_offset
        + written.message_descriptors[0].offset
        + written.message_descriptors[0].serialized_length,
    )
    middle_range = (
        header.messages_offset + written.message_descriptors[1].offset,
        header.messages_offset
        + written.message_descriptors[1].offset
        + written.message_descriptors[1].serialized_length,
    )
    assert all(
        not _ranges_overlap((offset, offset + length), first_range)
        and not _ranges_overlap((offset, offset + length), middle_range)
        for offset, length in spy.calls
        if offset >= header.messages_offset
    )
    assert any(
        _ranges_overlap((offset, offset + length), selected_range)
        for offset, length in spy.calls
    )
    assert any(offset == start and length == len(b"  - !!binary |\n") for offset, length in spy.calls)

    with pytest.raises(MessageDescriptorError):
        copy_message(BytesRangeSource(data), header, selected, io.BytesIO(), expected_index=1)


def test_selected_message_detects_ciphertext_tampering_and_bad_descriptor_bounds() -> None:
    data, written = _container(messages=(b"ciphertext to tamper with",))
    header = read_metadata(BytesRangeSource(data))
    descriptor = written.message_descriptors[0]
    body_start = header.messages_offset + descriptor.offset + len(b"  - !!binary |\n")
    tampered = bytearray(data)
    tampered[body_start] = ord("Z") if tampered[body_start] != ord("Z") else ord("A")
    with pytest.raises(MessageDescriptorError):
        copy_message(BytesRangeSource(bytes(tampered)), header, descriptor, io.BytesIO())

    malformed = type(descriptor)(
        message_index=descriptor.message_index,
        offset=descriptor.offset,
        serialized_length=descriptor.serialized_length + 1,
        ciphertext_length=descriptor.ciphertext_length,
        sha256=descriptor.sha256,
    )
    with pytest.raises(MessageDescriptorError):
        copy_message(BytesRangeSource(data), header, malformed, io.BytesIO())


def test_strict_header_rejects_noncanonical_yaml_and_unknown_version() -> None:
    data, _ = _container()
    with pytest.raises(ContainerFormatError):
        read_metadata(BytesRangeSource(data.replace(b"metadata: !!binary |", b"metadata: !evil |")))

    duplicate = data.replace(
        b"version: 1\n",
        b"version: 1\nversion: 1\n",
    )
    with pytest.raises(ContainerFormatError):
        read_metadata(BytesRangeSource(duplicate))

    alias = data.replace(b"recipient_hints:\n", b"recipient_hints: &hints\n", 1)
    with pytest.raises(ContainerFormatError):
        read_metadata(BytesRangeSource(alias))

    extra_document = data + b"---\nformat: localsetup.secure-container\n"
    with pytest.raises(ContainerFormatError):
        scan_message_descriptors(BytesRangeSource(extra_document))

    future = data.replace(b"version: 1\n", b"version: 3\n", 1)
    with pytest.raises(UnsupportedContainerVersion) as unsupported:
        read_metadata(BytesRangeSource(future))
    assert unsupported.value.version == 3

    duplicate_hint = data.replace(
        b"  - 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'\n",
        b"  - 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n",
    )
    with pytest.raises(ContainerFormatError):
        read_metadata(BytesRangeSource(duplicate_hint))
    reversed_hints = data.replace(
        b"  - 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n"
        b"  - 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'\n",
        b"  - 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'\n"
        b"  - 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n",
    )
    with pytest.raises(ContainerFormatError):
        read_metadata(BytesRangeSource(reversed_hints))


def test_limits_are_configurable_and_line_bounds_fail_explicitly() -> None:
    data, _ = _container(metadata=b"x" * 100)
    with pytest.raises(ContainerLimitError):
        read_metadata(BytesRangeSource(data), limits=ContainerLimits(max_metadata_bytes=99))
    with pytest.raises(ContainerLimitError):
        read_metadata(BytesRangeSource(data), limits=ContainerLimits(max_line_bytes=8))
    with pytest.raises(ContainerLimitError):
        write_container(io.BytesIO(), recipient_hints=(RECIPIENT_A,),
                        metadata_ciphertext=b"cipher", messages=(b"message",),
                        limits=ContainerLimits(max_public_header_bytes=16))


def test_local_file_source_rejects_symlinks_changes_and_non_regular_files(tmp_path: Path) -> None:
    target = tmp_path / "ciphertext.lssc"
    target.write_bytes(b"first bytes")
    with LocalFileRangeSource(target) as source:
        assert source.read_range(0, 5) == b"first"

    changed = LocalFileRangeSource(target)
    target.write_bytes(b"other bytes")
    with pytest.raises(SourceChangedError):
        changed.read_range(0, 5)
    changed.close()

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ContainerSourceError):
        LocalFileRangeSource(directory)

    if hasattr(os, "symlink"):
        link = tmp_path / "link.lssc"
        link.symlink_to(target)
        with pytest.raises(ContainerSourceError):
            LocalFileRangeSource(link)

        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "ciphertext.lssc").write_bytes(b"bytes")
        parent_link = tmp_path / "parent-link"
        parent_link.symlink_to(parent, target_is_directory=True)
        with pytest.raises(ContainerSourceError):
            LocalFileRangeSource(parent_link / "ciphertext.lssc")

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo.lssc"
        os.mkfifo(fifo)
        with pytest.raises(ContainerSourceError):
            LocalFileRangeSource(fifo)


@pytest.mark.parametrize("length", [54, 55, 56, 57, 58, 112, 113])
def test_base64_width_boundaries_roundtrip_metadata_scan_and_selected_copy(length: int) -> None:
    metadata = bytes(index % 251 for index in range(length))
    message = bytes((index * 3) % 251 for index in range(length))
    output = io.BytesIO()
    written = write_container(
        output,
        recipient_hints=(RECIPIENT_A,),
        metadata_ciphertext=metadata,
        messages=(message,),
    )
    data = output.getvalue()
    assert yaml.safe_load(data)["metadata"] == metadata
    header = read_metadata(BytesRangeSource(data))
    assert header.encrypted_metadata == metadata
    assert scan_message_descriptors(BytesRangeSource(data)) == written.message_descriptors
    selected = io.BytesIO()
    assert copy_message(
        BytesRangeSource(data),
        header,
        written.message_descriptors[0],
        selected,
    ) == length
    assert selected.getvalue() == message


def test_protected_schema_dispatches_required_features_and_ignores_bounded_extensions() -> None:
    base = {
        "schema": PROTECTED_METADATA_SCHEMA,
        "major": 1,
        "minor": 99,
        "required_features": [],
        "extensions": {"future-field": {"mode": "opaque", "values": [1, True]}},
    }
    view = validate_protected_schema(base)
    assert view.major == 1
    assert view.minor == 99
    assert view.ignored_optional_fields == ("future-field",)

    with pytest.raises(UnsupportedProtectedSchemaVersion):
        validate_protected_schema({**base, "major": 2})
    with pytest.raises(UnsupportedRequiredFeature):
        validate_protected_schema({**base, "required_features": ["future-required"]})
    with pytest.raises(ProtectedSchemaError):
        validate_protected_schema({**base, "extensions": {"too-large": "x" * 100}}, max_extension_bytes=20)
    with pytest.raises(ProtectedSchemaError):
        validate_protected_schema({**base, "unbounded-top-level": True})


def _ranges_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]

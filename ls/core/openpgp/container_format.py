"""Canonical streaming framing for LocalSetup Secure Container bytes.

This module handles YAML framing and opaque ciphertext only. It performs no
encryption, signature verification, trust decision, enrollment, or acceptance.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator

from .container_sources import ContainerSourceError, RangeSource, SourceChangedError
from .contracts import OpenPGPContractError, normalize_fingerprint


CONTAINER_FORMAT = "localsetup.secure-container"
CONTAINER_VERSION = 1
PRIVATE_CONTAINER_VERSION = 2
_FORMAT_LINE = b"format: localsetup.secure-container\n"
_RECIPIENTS_LINE = b"recipient_hints:\n"
_METADATA_MARKER = b"metadata: !!binary |\n"
_MESSAGES_HEADER = b"messages:\n"
_MESSAGE_MARKER = b"  - !!binary |\n"
_BASE64_WIDTH = 76
_FULL_FINGERPRINT = re.compile(rb"[0-9A-F]{40}\Z")
_VERSION_LINE = re.compile(rb"version: (0|[1-9][0-9]*)\n\Z")
_METADATA_LENGTH_LINE = re.compile(rb"metadata_length: (0|[1-9][0-9]*)\n\Z")


class ContainerFormatError(ValueError):
    """Malformed or noncanonical container framing."""


class ContainerLimitError(ContainerFormatError):
    """A configured practical processing limit was exceeded."""


class UnsupportedContainerVersion(ContainerFormatError):
    """The outer container format uses an unsupported version."""

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(f"unsupported secure-container version: {version}")


class MessageDescriptorError(ContainerFormatError):
    """A selected message did not match its expected descriptor."""


@dataclass(frozen=True)
class ContainerLimits:
    """Configurable operational bounds; these values do not define the format."""

    max_public_header_bytes: int = 64 * 1024
    max_metadata_bytes: int = 16 * 1024 * 1024
    max_message_bytes: int = 2 * 1024 * 1024 * 1024
    max_messages: int = 100_000
    max_recipient_hints: int = 256
    max_line_bytes: int = 4096
    io_chunk_bytes: int = 64 * 1024
    max_container_bytes: int = 8 * 1024 * 1024 * 1024
    max_files: int = 100_000
    max_volumes: int = 100_000
    max_total_segments: int = 1_000_000

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ContainerHeader:
    format: str
    version: int
    recipient_hints: tuple[str, ...]
    metadata_length: int
    encrypted_metadata: bytes
    messages_offset: int
    canonical_public_header: bytes

    @property
    def public_header_sha256(self) -> str:
        """Digest of canonical public fields, excluding ciphertext and payloads."""
        return hashlib.sha256(self.canonical_public_header).hexdigest()


@dataclass(frozen=True)
class MessageDescriptor:
    """Expected framing for one message, relative to the messages section."""

    message_index: int
    offset: int
    serialized_length: int
    ciphertext_length: int
    sha256: str

    def __post_init__(self) -> None:
        for name in ("message_index", "offset", "serialized_length", "ciphertext_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class ContainerWriteResult:
    message_descriptors: tuple[MessageDescriptor, ...]
    serialized_length: int
    public_header_sha256: str


class _CountingWriter:
    def __init__(self, destination: BinaryIO, limit: int) -> None:
        self.destination = destination
        self.limit = limit
        self.count = 0

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        if self.count + len(view) > self.limit:
            raise ContainerLimitError("serialized container exceeds the configured limit")
        while view:
            written = self.destination.write(view)
            if not isinstance(written, int) or written <= 0:
                raise OSError("container destination did not accept all bytes")
            self.count += written
            view = view[written:]


def write_container(
    destination: BinaryIO,
    *,
    recipient_hints: Iterable[str],
    metadata_ciphertext: bytes,
    messages: Iterable[bytes | BinaryIO],
    version: int = CONTAINER_VERSION,
    limits: ContainerLimits = ContainerLimits(),
) -> ContainerWriteResult:
    """Write deterministic YAML framing around opaque ciphertext streams.

    Message descriptors cover the exact YAML item length and ciphertext digest.
    They are not authenticated by this function; a higher layer must protect
    the descriptor inventory inside its independently encrypted metadata.
    The legacy default writes v1. Explicit v2 omits recipient hints and requires
    an empty iterable so callers cannot accidentally disclose an audience.
    """
    if not isinstance(metadata_ciphertext, bytes) or not metadata_ciphertext:
        raise ContainerFormatError("encrypted metadata must be non-empty bytes")
    if len(metadata_ciphertext) > limits.max_metadata_bytes:
        raise ContainerLimitError("encrypted metadata exceeds the configured limit")
    if type(version) is not int:
        raise ContainerFormatError("container version must be an integer")
    if version not in (CONTAINER_VERSION, PRIVATE_CONTAINER_VERSION):
        raise UnsupportedContainerVersion(version)
    if version == CONTAINER_VERSION:
        hints = _normalize_hints(recipient_hints, limits)
    else:
        try:
            has_hints = any(True for _ in recipient_hints)
        except TypeError as exc:
            raise ContainerFormatError("recipient hints must be iterable") from exc
        if has_hints:
            raise ContainerFormatError("container v2 must not contain recipient hints")
        hints = ()
    writer = _CountingWriter(destination, limits.max_container_bytes)
    public_hash = hashlib.sha256()
    public_size = 0

    def public(data: bytes) -> None:
        nonlocal public_size
        public_size += len(data)
        if public_size > limits.max_public_header_bytes:
            raise ContainerLimitError("public header exceeds the configured limit")
        writer.write(data)
        public_hash.update(data)

    public(_FORMAT_LINE)
    public(f"version: {version}\n".encode("ascii"))
    if version == CONTAINER_VERSION:
        public(_RECIPIENTS_LINE)
        for hint in hints:
            public(b"  - '" + hint.encode("ascii") + b"'\n")
    public(f"metadata_length: {len(metadata_ciphertext)}\n".encode("ascii"))
    public(_METADATA_MARKER)
    _write_binary_bytes(writer, metadata_ciphertext, indent=b"  ", limits=limits)

    messages_offset = writer.count
    writer.write(_MESSAGES_HEADER)
    descriptors: list[MessageDescriptor] = []
    try:
        iterator = iter(messages)
    except TypeError as exc:
        raise ContainerFormatError("messages must be an iterable of byte streams") from exc
    for index, message in enumerate(iterator):
        if index >= limits.max_messages:
            raise ContainerLimitError("message count exceeds the configured limit")
        start = writer.count
        message_offset = start - messages_offset
        writer.write(_MESSAGE_MARKER)
        length, digest = _write_binary_stream(writer, message, indent=b"    ", limits=limits)
        if length == 0:
            raise ContainerFormatError("encrypted messages must be non-empty")
        descriptors.append(
            MessageDescriptor(
                message_index=index,
                offset=message_offset,
                serialized_length=writer.count - start,
                ciphertext_length=length,
                sha256=digest,
            )
        )
    if not descriptors:
        raise ContainerFormatError("a secure container must contain at least one message")
    return ContainerWriteResult(
        message_descriptors=tuple(descriptors),
        serialized_length=writer.count,
        public_header_sha256=public_hash.hexdigest(),
    )


def read_metadata(
    source: RangeSource,
    *,
    limits: ContainerLimits = ContainerLimits(),
) -> ContainerHeader:
    """Read only the bounded public header and encrypted metadata block.

    The final requested range ends immediately after the messages section
    header. No message body bytes are requested. The returned metadata remains
    ciphertext and must not be treated as authenticated or trusted.
    """
    source_size = _source_size(source)
    position = 0
    public_header = bytearray()

    first = _read_exact(source, source_size, position, len(_FORMAT_LINE))
    if first != _FORMAT_LINE:
        raise ContainerFormatError("invalid or noncanonical container format line")
    position += len(first)
    public_header.extend(first)

    version_line, position = _read_line(source, source_size, position, limits.max_line_bytes)
    match = _VERSION_LINE.fullmatch(version_line)
    if match is None:
        raise ContainerFormatError("invalid or noncanonical container version line")
    version = int(match.group(1))
    public_header.extend(version_line)
    if version not in (CONTAINER_VERSION, PRIVATE_CONTAINER_VERSION):
        raise UnsupportedContainerVersion(version)

    hints: list[str] = []
    if version == CONTAINER_VERSION:
        recipients = _read_exact(source, source_size, position, len(_RECIPIENTS_LINE))
        if recipients != _RECIPIENTS_LINE:
            raise ContainerFormatError("invalid or noncanonical recipient-hints header")
        position += len(recipients)
        public_header.extend(recipients)
    while True:
        probe = _read_exact(source, source_size, position, 5)
        if version == CONTAINER_VERSION and probe == b"  - '":
            line = probe + _read_exact(source, source_size, position + 5, 42)
            if len(hints) >= limits.max_recipient_hints:
                raise ContainerLimitError("recipient-hint count exceeds the configured limit")
            match = re.fullmatch(rb"  - '([0-9A-F]{40})'\n", line)
            if match is None:
                raise ContainerFormatError("invalid or noncanonical recipient hint")
            hints.append(match.group(1).decode("ascii"))
            position += len(line)
            public_header.extend(line)
            _check_public_header_size(public_header, limits)
            continue
        line, position = _read_line_with_prefix(
            source,
            source_size,
            position,
            probe,
            limits.max_line_bytes,
        )
        match = _METADATA_LENGTH_LINE.fullmatch(line)
        if match is None:
            raise ContainerFormatError("invalid or noncanonical metadata-length line")
        metadata_length = int(match.group(1))
        public_header.extend(line)
        _check_public_header_size(public_header, limits)
        break
    if version == CONTAINER_VERSION and not hints:
        raise ContainerFormatError("at least one recipient hint is required")
    if hints != sorted(set(hints)):
        raise ContainerFormatError("recipient hints are not in canonical sorted unique order")
    if metadata_length <= 0:
        raise ContainerFormatError("encrypted metadata length must be positive")
    if metadata_length > limits.max_metadata_bytes:
        raise ContainerLimitError("encrypted metadata exceeds the configured limit")

    marker = _read_exact(source, source_size, position, len(_METADATA_MARKER))
    if marker != _METADATA_MARKER:
        raise ContainerFormatError("invalid or noncanonical metadata block marker")
    position += len(marker)
    public_header.extend(marker)
    _check_public_header_size(public_header, limits)

    encrypted_metadata, position = _read_binary_block(
        source,
        source_size,
        position,
        metadata_length,
        indent=b"  ",
        limits=limits,
        max_ciphertext_bytes=limits.max_metadata_bytes,
    )
    messages_offset = position
    header = _read_exact(source, source_size, position, len(_MESSAGES_HEADER))
    if header != _MESSAGES_HEADER:
        raise ContainerFormatError("invalid or noncanonical messages section header")
    position += len(header)
    if position >= source_size:
        raise ContainerFormatError("container ends before the messages section")
    if _source_size(source) != source_size:
        raise SourceChangedError("source size changed while reading metadata")
    return ContainerHeader(
        format=CONTAINER_FORMAT,
        version=version,
        recipient_hints=tuple(hints),
        metadata_length=metadata_length,
        encrypted_metadata=encrypted_metadata,
        messages_offset=messages_offset,
        canonical_public_header=bytes(public_header),
    )


def copy_message(
    source: RangeSource,
    header: ContainerHeader,
    authenticated_descriptor: MessageDescriptor,
    destination: BinaryIO,
    *,
    expected_index: int | None = None,
    limits: ContainerLimits = ContainerLimits(),
) -> int:
    """Copy one selected ciphertext range after checking its expected framing.

    The descriptor is an expectation supplied by the caller after independent
    authentication of protected metadata. This function checks the selected
    bytes against it but cannot authenticate that metadata or make a trust
    decision. Output may contain partial data if validation fails; callers
    should write to protected temporary storage and publish only after success.
    """
    descriptor = authenticated_descriptor
    if not isinstance(descriptor, MessageDescriptor):
        raise TypeError("authenticated_descriptor must be a MessageDescriptor")
    if expected_index is not None and descriptor.message_index != expected_index:
        raise MessageDescriptorError("selected message index does not match its descriptor")
    if descriptor.message_index >= limits.max_messages:
        raise ContainerLimitError("message index exceeds the configured limit")
    if descriptor.ciphertext_length <= 0 or descriptor.ciphertext_length > limits.max_message_bytes:
        raise ContainerLimitError("message length is outside configured limits")
    expected_serialized = len(_MESSAGE_MARKER) + _binary_block_serialized_length(
        descriptor.ciphertext_length, indent=b"    "
    )
    if descriptor.serialized_length != expected_serialized:
        raise MessageDescriptorError("serialized message length does not match ciphertext length")
    if descriptor.offset < len(_MESSAGES_HEADER):
        raise MessageDescriptorError("message offset is before the first sequence entry")

    source_size = _source_size(source)
    observed_header = read_metadata(source, limits=limits)
    if (
        observed_header.canonical_public_header != header.canonical_public_header
        or observed_header.encrypted_metadata != header.encrypted_metadata
        or observed_header.messages_offset != header.messages_offset
    ):
        raise SourceChangedError("container header changed after metadata inspection")
    start = header.messages_offset + descriptor.offset
    end = start + descriptor.serialized_length
    if end > source_size or end < start:
        raise MessageDescriptorError("selected message range is outside the source")
    marker = _read_exact(source, source_size, start, len(_MESSAGE_MARKER))
    if marker != _MESSAGE_MARKER:
        raise MessageDescriptorError("selected range is not a canonical message entry")
    output_hash = hashlib.sha256()

    def write_checked(data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = destination.write(view)
            if not isinstance(written, int) or written <= 0:
                raise OSError("message destination did not accept all bytes")
            view = view[written:]

    actual_length = _copy_binary_block_to(
        source,
        source_size,
        start + len(_MESSAGE_MARKER),
        descriptor.ciphertext_length,
        indent=b"    ",
        limits=limits,
        sink=lambda block: (output_hash.update(block), write_checked(block)),
    )
    consumed = len(_MESSAGE_MARKER) + _binary_block_serialized_length(
        descriptor.ciphertext_length, indent=b"    "
    )
    if consumed != descriptor.serialized_length or actual_length != descriptor.ciphertext_length:
        raise MessageDescriptorError("selected message framing length does not match descriptor")
    if output_hash.hexdigest() != descriptor.sha256:
        raise MessageDescriptorError("selected ciphertext does not match its SHA-256 descriptor")
    if _source_size(source) != source_size:
        raise SourceChangedError("source size changed while copying a selected message")
    return actual_length


def scan_message_descriptors(
    source: RangeSource,
    *,
    header: ContainerHeader | None = None,
    limits: ContainerLimits = ContainerLimits(),
) -> tuple[MessageDescriptor, ...]:
    """Validate all remaining canonical framing with bounded memory.

    This full validator reads the message bodies incrementally to verify base64
    spelling and compute ciphertext descriptors. Use read_metadata or
    copy_message when bulk access is not permitted.
    """
    observed_header = read_metadata(source, limits=limits)
    if header is not None and (
        observed_header.canonical_public_header != header.canonical_public_header
        or observed_header.encrypted_metadata != header.encrypted_metadata
        or observed_header.messages_offset != header.messages_offset
    ):
        raise SourceChangedError("container header changed after metadata inspection")
    header = observed_header
    source_size = _source_size(source)
    if source_size > limits.max_container_bytes:
        raise ContainerLimitError("container exceeds the configured total-size limit")
    position = header.messages_offset + len(_MESSAGES_HEADER)
    descriptors: list[MessageDescriptor] = []
    pending_line: tuple[bytes, int, int] | None = None

    while position < source_size or pending_line is not None:
        if len(descriptors) >= limits.max_messages:
            raise ContainerLimitError("message count exceeds the configured limit")
        if pending_line is None:
            first_line, next_position = _read_stream_line(
                source, source_size, position, limits
            )
            start = position
        else:
            first_line, start, next_position = pending_line
            pending_line = None
        if first_line != _MESSAGE_MARKER:
            raise ContainerFormatError("expected a canonical messages sequence entry")
        position = next_position
        digest = hashlib.sha256()
        ciphertext_length = 0
        previous_short = False
        previous_padded = False
        saw_binary_line = False
        while position < source_size:
            line, next_position = _read_stream_line(source, source_size, position, limits)
            if line == _MESSAGE_MARKER:
                pending_line = (line, position, next_position)
                break
            if line.startswith(b"  -"):
                raise ContainerFormatError("invalid or noncanonical message sequence marker")
            if previous_padded:
                raise ContainerFormatError("padded base64 line must be the final message line")
            encoded = _parse_binary_line(line, indent=b"    ")
            if previous_short:
                raise ContainerFormatError("only the final base64 line may be short")
            decoded = _decode_canonical_base64(encoded)
            if len(encoded) == _BASE64_WIDTH and b"=" not in encoded and len(decoded) != 57:
                raise ContainerFormatError("unpadded full-width base64 lines must encode 57 bytes")
            if (
                len(encoded) == _BASE64_WIDTH
                and b"=" in encoded
                and len(decoded) not in (55, 56)
            ):
                raise ContainerFormatError("padded full-width base64 lines must encode 55 or 56 bytes")
            previous_short = len(encoded) < _BASE64_WIDTH
            previous_padded = b"=" in encoded
            saw_binary_line = True
            ciphertext_length += len(decoded)
            if ciphertext_length > limits.max_message_bytes:
                raise ContainerLimitError("message exceeds the configured size limit")
            digest.update(decoded)
            position = next_position
        if not saw_binary_line or ciphertext_length <= 0:
            raise ContainerFormatError("encrypted messages must contain non-empty ciphertext")
        end = pending_line[1] if pending_line is not None else position
        serialized_length = end - start
        if serialized_length != len(_MESSAGE_MARKER) + _binary_block_serialized_length(
            ciphertext_length, indent=b"    "
        ):
            raise ContainerFormatError("message serialized length is noncanonical")
        descriptors.append(
            MessageDescriptor(
                message_index=len(descriptors),
                offset=start - header.messages_offset,
                serialized_length=serialized_length,
                ciphertext_length=ciphertext_length,
                sha256=digest.hexdigest(),
            )
        )
        if pending_line is not None:
            position = pending_line[1]
        elif position != source_size:
            raise ContainerFormatError("trailing bytes after the final message")

    if not descriptors:
        raise ContainerFormatError("a secure container must contain at least one message")
    if _source_size(source) != source_size:
        raise SourceChangedError("source size changed during container validation")
    return tuple(descriptors)


def _normalize_hints(values: Iterable[str], limits: ContainerLimits) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise ContainerFormatError("recipient hints must be an iterable of full fingerprints")
    normalized: list[str] = []
    try:
        for value in values:
            if len(normalized) >= limits.max_recipient_hints:
                raise ContainerLimitError("recipient-hint count exceeds the configured limit")
            normalized.append(normalize_fingerprint(value))
    except OpenPGPContractError as exc:
        raise ContainerFormatError("recipient hint is not an approved full fingerprint") from exc
    if not normalized:
        raise ContainerFormatError("at least one recipient hint is required")
    if len(set(normalized)) != len(normalized):
        raise ContainerFormatError("duplicate recipient hints are not canonical")
    return tuple(sorted(normalized))


def _source_size(source: RangeSource) -> int:
    size = source.size
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ContainerSourceError("source size must be a non-negative integer")
    return size


def _read_exact(source: RangeSource, source_size: int, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or offset + length > source_size:
        raise ContainerFormatError("container ends inside a required range")
    try:
        data = source.read_range(offset, length)
    except ContainerSourceError:
        raise
    if not isinstance(data, bytes) or len(data) != length:
        raise ContainerSourceError("range source returned an incomplete range")
    return data


def _read_line(
    source: RangeSource,
    source_size: int,
    offset: int,
    max_line_bytes: int,
) -> tuple[bytes, int]:
    return _read_line_with_prefix(source, source_size, offset, b"", max_line_bytes)


def _read_line_with_prefix(
    source: RangeSource,
    source_size: int,
    offset: int,
    prefix: bytes,
    max_line_bytes: int,
) -> tuple[bytes, int]:
    data = bytearray(prefix)
    position = offset + len(prefix)
    if len(data) > max_line_bytes:
        raise ContainerLimitError("line exceeds the configured length limit")
    while len(data) < max_line_bytes and position < source_size:
        byte = _read_exact(source, source_size, position, 1)
        data.extend(byte)
        position += 1
        if byte == b"\n":
            return bytes(data), position
    if len(data) >= max_line_bytes:
        raise ContainerLimitError("line exceeds the configured length limit")
    raise ContainerFormatError("container contains an unterminated header line")


def _check_public_header_size(header: bytearray, limits: ContainerLimits) -> None:
    if len(header) > limits.max_public_header_bytes:
        raise ContainerLimitError("public header exceeds the configured size limit")


def _binary_block_serialized_length(ciphertext_length: int, *, indent: bytes) -> int:
    encoded_chars = 4 * ((ciphertext_length + 2) // 3)
    line_count = (encoded_chars + _BASE64_WIDTH - 1) // _BASE64_WIDTH
    return len(indent) * line_count + encoded_chars + line_count


def _read_binary_block(
    source: RangeSource,
    source_size: int,
    offset: int,
    ciphertext_length: int,
    *,
    indent: bytes,
    limits: ContainerLimits,
    max_ciphertext_bytes: int,
) -> tuple[bytes, int]:
    if ciphertext_length > max_ciphertext_bytes:
        raise ContainerLimitError("binary block exceeds the configured size limit")
    result = bytearray()
    end = offset + _binary_block_serialized_length(ciphertext_length, indent=indent)
    if end > source_size:
        raise ContainerFormatError("container ends inside a binary block")
    position = offset
    remaining_chars = 4 * ((ciphertext_length + 2) // 3)
    lines_per_group = max(1, limits.io_chunk_bytes // (len(indent) + _BASE64_WIDTH + 1))
    chars_left = remaining_chars
    while chars_left:
        line_count = min(lines_per_group, (chars_left + _BASE64_WIDTH - 1) // _BASE64_WIDTH)
        group_chars = min(chars_left, line_count * _BASE64_WIDTH)
        group_bytes = len(indent) * line_count + group_chars + line_count
        raw = _read_exact(source, source_size, position, group_bytes)
        cursor = 0
        chars_in_group = group_chars
        for _ in range(line_count):
            count = min(chars_in_group, _BASE64_WIDTH)
            line_size = len(indent) + count + 1
            line = raw[cursor : cursor + line_size]
            if not line.startswith(indent) or line[-1:] != b"\n":
                raise ContainerFormatError("invalid or noncanonical base64 block line")
            encoded = line[len(indent) : -1]
            decoded = _decode_canonical_base64(encoded)
            expected_line_bytes = min(57, ciphertext_length - len(result))
            if len(decoded) != expected_line_bytes:
                raise ContainerFormatError("base64 line length disagrees with metadata framing")
            result.extend(decoded)
            cursor += line_size
            chars_in_group -= count
        if cursor != len(raw):
            raise ContainerFormatError("unexpected bytes in base64 block")
        position += group_bytes
        chars_left -= group_chars
    if len(result) != ciphertext_length:
        raise ContainerFormatError("binary block length does not match its framing length")
    return bytes(result), position


def _copy_binary_block_to(
    source: RangeSource,
    source_size: int,
    offset: int,
    ciphertext_length: int,
    *,
    indent: bytes,
    limits: ContainerLimits,
    sink,
) -> int:
    expected_end = offset + _binary_block_serialized_length(ciphertext_length, indent=indent)
    if expected_end > source_size:
        raise MessageDescriptorError("selected message range ends inside its ciphertext")
    position = offset
    remaining_chars = 4 * ((ciphertext_length + 2) // 3)
    lines_per_group = max(1, limits.io_chunk_bytes // (len(indent) + _BASE64_WIDTH + 1))
    chars_left = remaining_chars
    decoded_total = 0
    while chars_left:
        line_count = min(lines_per_group, (chars_left + _BASE64_WIDTH - 1) // _BASE64_WIDTH)
        group_chars = min(chars_left, line_count * _BASE64_WIDTH)
        group_bytes = len(indent) * line_count + group_chars + line_count
        raw = _read_exact(source, source_size, position, group_bytes)
        cursor = 0
        chars_in_group = group_chars
        for _ in range(line_count):
            count = min(chars_in_group, _BASE64_WIDTH)
            line_size = len(indent) + count + 1
            line = raw[cursor : cursor + line_size]
            if not line.startswith(indent) or line[-1:] != b"\n":
                raise MessageDescriptorError("selected message has invalid base64 framing")
            encoded = line[len(indent) : -1]
            decoded = _decode_canonical_base64(encoded)
            expected_line_bytes = min(57, ciphertext_length - decoded_total)
            if len(decoded) != expected_line_bytes:
                raise MessageDescriptorError("base64 line length disagrees with message descriptor")
            decoded_total += len(decoded)
            if decoded_total > ciphertext_length:
                raise MessageDescriptorError("selected ciphertext exceeds its descriptor length")
            sink(decoded)
            cursor += line_size
            chars_in_group -= count
        if cursor != len(raw):
            raise MessageDescriptorError("selected message has trailing framing bytes")
        position += group_bytes
        chars_left -= group_chars
    if decoded_total != ciphertext_length or position != expected_end:
        raise MessageDescriptorError("selected message range length is inconsistent")
    return decoded_total


def _decode_canonical_base64(encoded: bytes) -> bytes:
    if not encoded or len(encoded) > _BASE64_WIDTH or len(encoded) % 4:
        raise ContainerFormatError("base64 line has a noncanonical length")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ContainerFormatError("binary block contains invalid base64") from exc
    if base64.b64encode(decoded) != encoded:
        raise ContainerFormatError("binary block uses noncanonical base64")
    return decoded


def _parse_binary_line(line: bytes, *, indent: bytes) -> bytes:
    if not line.endswith(b"\n") or not line.startswith(indent):
        raise ContainerFormatError("binary block line has invalid indentation or terminator")
    encoded = line[len(indent) : -1]
    if not encoded or len(encoded) > _BASE64_WIDTH or len(encoded) % 4:
        raise ContainerFormatError("binary block line has a noncanonical width")
    return encoded


def _read_stream_line(
    source: RangeSource,
    source_size: int,
    offset: int,
    limits: ContainerLimits,
) -> tuple[bytes, int]:
    if offset >= source_size:
        raise ContainerFormatError("expected a line before end of container")
    collected = bytearray()
    position = offset
    while position < source_size:
        request = min(limits.io_chunk_bytes, limits.max_line_bytes + 1 - len(collected), source_size - position)
        if request <= 0:
            raise ContainerLimitError("line exceeds the configured length limit")
        chunk = _read_exact(source, source_size, position, request)
        newline = chunk.find(b"\n")
        if newline >= 0:
            collected.extend(chunk[: newline + 1])
            if len(collected) > limits.max_line_bytes:
                raise ContainerLimitError("line exceeds the configured length limit")
            return bytes(collected), position + newline + 1
        collected.extend(chunk)
        position += len(chunk)
        if len(collected) >= limits.max_line_bytes:
            raise ContainerLimitError("line exceeds the configured length limit")
    raise ContainerFormatError("container ends with an unterminated line")


def _write_binary_bytes(
    writer: _CountingWriter,
    value: bytes,
    *,
    indent: bytes,
    limits: ContainerLimits,
) -> None:
    for start in range(0, len(value), max(57, (limits.io_chunk_bytes // 57) * 57)):
        _write_base64_batch(writer, value[start : start + max(57, (limits.io_chunk_bytes // 57) * 57)], indent)


def _write_binary_stream(
    writer: _CountingWriter,
    message: bytes | BinaryIO,
    *,
    indent: bytes,
    limits: ContainerLimits,
) -> tuple[int, str]:
    if isinstance(message, bytes):
        blocks: Iterator[bytes] = iter(
            message[start : start + limits.io_chunk_bytes]
            for start in range(0, len(message), limits.io_chunk_bytes)
        )
    elif hasattr(message, "read"):
        def read_blocks() -> Iterator[bytes]:
            while True:
                block = message.read(limits.io_chunk_bytes)
                if block == b"":
                    return
                if not isinstance(block, bytes):
                    raise ContainerFormatError("message stream must return bytes")
                yield block

        blocks = read_blocks()
    else:
        raise ContainerFormatError("each message must be bytes or a binary stream")

    digest = hashlib.sha256()
    total = 0
    carry = bytearray()
    batch_lines: list[bytes] = []
    batch_size = 0
    for block in blocks:
        if not block:
            continue
        total += len(block)
        if total > limits.max_message_bytes:
            raise ContainerLimitError("message exceeds the configured size limit")
        digest.update(block)
        carry.extend(block)
        complete = (len(carry) // 57) * 57
        if complete:
            encoded = base64.b64encode(carry[:complete])
            del carry[:complete]
            batch_lines.extend(
                indent + encoded[start : start + _BASE64_WIDTH] + b"\n"
                for start in range(0, len(encoded), _BASE64_WIDTH)
            )
            batch_size += len(encoded) // _BASE64_WIDTH * (len(indent) + _BASE64_WIDTH + 1)
            if batch_size >= limits.io_chunk_bytes:
                writer.write(b"".join(batch_lines))
                batch_lines.clear()
                batch_size = 0
    if carry:
        batch_lines.append(indent + base64.b64encode(carry) + b"\n")
    if batch_lines:
        writer.write(b"".join(batch_lines))
    return total, digest.hexdigest()


def _write_base64_batch(writer: _CountingWriter, value: bytes, indent: bytes) -> None:
    if not value:
        return
    encoded = base64.b64encode(value)
    writer.write(
        b"".join(
            indent + encoded[start : start + _BASE64_WIDTH] + b"\n"
            for start in range(0, len(encoded), _BASE64_WIDTH)
        )
    )

"""Grant-bound paging for one verified container file or metadata index."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets

from ..openpgp.container_reader import ContainerReadError, ContainerReader, VerifiedContainerRead


def validate_reader(reader, workspace):
    if reader is None:
        return
    if not isinstance(reader, ContainerReader):
        raise ValueError('Protected reads require operator-bound authority')
    workspace = workspace.resolve()
    for private in (Path(reader.policy.boundary), reader.trust_state_path, reader.gnupg_home):
        private = private.resolve()
        if private.is_relative_to(workspace) or workspace.is_relative_to(private):
            raise ValueError('Protected read state and granted workspace must be separate')


def container_candidate(prefix):
    return prefix.startswith(b'format: localsetup.secure-container\n')


def file_reference(name):
    """Split only the reserved member suffix returned by the verified index."""
    base, marker, selected = name.rpartition('/@files/')
    if marker and base and len(selected) == 32 and all(ch in '0123456789abcdef' for ch in selected):
        return base, selected
    return name, None


@dataclass(slots=True, repr=False)
class ContainerPages:
    reader: ContainerReader
    opened: VerifiedContainerRead
    authority: str
    revision: str
    nonce: str
    next_cursor: str | None = None
    count: int = 0

    def close(self):
        self.opened.close()


def read_container_page(broker, *, task, session, name, source_name, file_id,
                        cursor, authority, before, for_provider):
    """The calling broker holds its grant lease and opened carrier descriptor."""
    reader = broker.container_reader
    if not isinstance(reader, ContainerReader):
        raise ContainerReadError('unauthorized')
    def check():
        broker.grant.check(task, session, 'read', name, provider=for_provider)
    cache = broker._container_page_cache
    if cursor is None:
        broker._wipe_container_page_cache()
        if source_name is None:
            if reader.reference_resolver is None:
                raise ContainerReadError('unavailable')
            opened = reader.reference_resolver(name, check=check, deadline=broker.grant.expires)
        else:
            opened = reader.prepare(broker.grant.root / source_name, source_name, file_id=file_id,
                                    expected_stat=before, check=check, deadline=broker.grant.expires)
        revision = hashlib.sha256(json.dumps({
            'logical': opened.metadata.logical_commitment_sha256 if opened.metadata is not None else None,
            'file': file_id, 'content': opened.digest,
            'binding': opened.binding,
            'sources': [generation for _, generation in opened.sources],
        }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        cache = ContainerPages(reader, opened, authority, revision, secrets.token_urlsafe(16))
        broker._container_page_cache = cache
        offset = 0
    else:
        revision, offset, nonce = broker._decode_page_cursor(cursor, authority)
        if (cache is None or cache.reader is not reader or cache.authority != authority
                or cache.revision != revision or cache.nonce != nonce or cache.next_cursor != cursor):
            raise ContainerReadError('changed')
    if cache.count >= 10_000 or offset > cache.opened.size:
        raise ContainerReadError('limit_exceeded')
    raw = cache.opened.read(offset, 8192, check)
    response = broker._bounded_page_response(raw, text=cache.opened.text, offset=offset,
        size=cache.opened.size, authority=authority, revision=cache.revision, stream_nonce=cache.nonce)
    cache.opened.check(check)
    if not response['bytes'] and response['next_cursor'] is not None:
        raise ContainerReadError('unavailable')
    cache.count += 1
    cache.next_cursor = response['next_cursor']
    if cache.next_cursor is None:
        broker._wipe_container_page_cache()
    return response

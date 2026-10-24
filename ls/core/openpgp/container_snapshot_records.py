"""Protected evidence for complete snapshots of an exact reconstructed version."""
from dataclasses import asdict, dataclass
import hashlib
import re

from .container_delta_records import FileEvidence

SNAPSHOT_FEATURE = 'consolidated-snapshot-v1'
MAX_COVERED_FILES = 100_000


def _identity(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None or value == '0'*32:
        raise ValueError('Invalid snapshot identity')


@dataclass(frozen=True, slots=True)
class CoveredFile:
    version_id: str
    commitment_sha256: str
    file_id: str
    length: int
    sha256: str
    change_id: str | None = None

    def __post_init__(self):
        _identity(self.version_id)
        _identity(self.file_id)
        FileEvidence(0, self.commitment_sha256)
        FileEvidence(self.length, self.sha256)
        if self.change_id is not None:
            _identity(self.change_id)

    @property
    def key(self):
        return self.version_id, self.file_id


@dataclass(frozen=True, slots=True)
class SnapshotClaim:
    source_version: str
    source_commitment: str
    covered: tuple[CoveredFile, ...]

    def __post_init__(self):
        _identity(self.source_version)
        FileEvidence(0, self.source_commitment)
        if (type(self.covered) is not tuple or not 1 <= len(self.covered) <= MAX_COVERED_FILES
                or any(not isinstance(item, CoveredFile) for item in self.covered)):
            raise ValueError('Invalid snapshot coverage')
        keys = tuple(item.key for item in self.covered)
        if keys != tuple(sorted(set(keys))):
            raise ValueError('Snapshot coverage must be canonical and unique')
        commitments = {}
        for item in self.covered:
            previous = commitments.setdefault(item.version_id, item.commitment_sha256)
            if previous != item.commitment_sha256:
                raise ValueError('Conflicting snapshot coverage')
        if commitments.get(self.source_version) != self.source_commitment:
            raise ValueError('Snapshot source is not covered')

    def record(self):
        return {'source_version': self.source_version, 'source_commitment': self.source_commitment,
                'covered': [asdict(item) for item in self.covered]}

    @property
    def checkpoint_sha256(self):
        from .artifact_records import canonical_bytes
        return hashlib.sha256(canonical_bytes(self.record())).hexdigest()


def snapshot_claim(value):
    if (type(value) is not dict or set(value) != {'source_version', 'source_commitment', 'covered'}
            or type(value['covered']) is not list or not 1 <= len(value['covered']) <= MAX_COVERED_FILES):
        raise ValueError('Invalid snapshot record')
    fields = {'version_id', 'commitment_sha256', 'file_id', 'length', 'sha256', 'change_id'}
    if any(type(item) is not dict or set(item) != fields for item in value['covered']):
        raise ValueError('Invalid snapshot covered file')
    return SnapshotClaim(value['source_version'], value['source_commitment'],
                         tuple(CoveredFile(**item) for item in value['covered']))


def validate_snapshot(logical):
    claim = snapshot_claim(logical['snapshot'])
    if logical['parent_versions'] != [claim.source_version] or logical['version_id'] == claim.source_version:
        raise ValueError('Invalid snapshot ancestry')
    source_files = {item.file_id: (item.length, item.sha256) for item in claim.covered
                    if item.version_id == claim.source_version}
    result_files = {item['file_id']: (item['length'], item['sha256']) for item in logical['files']}
    if source_files != result_files or any('delta' in item for item in logical['files']):
        raise ValueError('Snapshot must preserve every source file as complete content')
    if any(item.version_id == logical['version_id'] for item in claim.covered):
        raise ValueError('Snapshot cannot cover itself')

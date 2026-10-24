"""Immutable per-file delta claims, authenticated by enclosing container metadata."""
from dataclasses import asdict, dataclass
import re


DELTA_FEATURE = "per-file-delta-v1"
DELTA_CODEC = "zstd-patch-v1"


@dataclass(frozen=True, slots=True)
class FileEvidence:
    length: int
    sha256: str

    def __post_init__(self):
        if (type(self.length) is not int or not 0 <= self.length <= 2**63-1
                or type(self.sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None):
            raise ValueError("Invalid file evidence")


@dataclass(frozen=True, slots=True)
class FileDelta:
    base_version: str
    base_commitment: str
    base_file_id: str
    base: FileEvidence
    patch: FileEvidence
    result: FileEvidence
    change_id: str
    codec: str = DELTA_CODEC

    def __post_init__(self):
        for value in (self.base_version, self.base_file_id, self.change_id):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None or value == '0'*32:
                raise ValueError("Invalid delta identity")
        if (type(self.base_commitment) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.base_commitment) is None
                or any(not isinstance(item, FileEvidence) for item in (self.base, self.patch, self.result))
                or self.patch.length == 0 or self.codec != DELTA_CODEC):
            raise ValueError("Invalid delta claim")

    def record(self):
        # Result evidence remains in the ordinary logical file descriptor.
        value = asdict(self)
        value.pop('result')
        return value


def file_delta(file):
    value = file.get('delta')
    if not isinstance(value, dict) or set(value) != {
            'base_version', 'base_commitment', 'base_file_id', 'base', 'patch', 'change_id', 'codec'}:
        raise ValueError("Invalid delta record")
    return FileDelta(**(value | {'base': FileEvidence(**value['base']),
        'patch': FileEvidence(**value['patch']), 'result': FileEvidence(file['length'], file['sha256'])}))


def stored_evidence(file):
    return file_delta(file).patch if 'delta' in file else FileEvidence(file['length'], file['sha256'])

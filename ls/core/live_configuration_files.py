"""Private editable owner files feeding the shared signed configuration runtime."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

from .live_configuration import ConfigurationError, ConfigurationStore, _MAX_PAYLOAD, _digest, _encode
from .live_configuration_runtime import ConfigurationRuntime
from .openpgp.container_storage import ensure_private_directory, write_private_file
from .openpgp.envelope_io import AnchoredFile
from .openpgp.envelope import EnvelopeError

_SCHEMA = 'localsetup.configuration-edit'


def editable_json(raw):
    """Accept human formatting, but never duplicate properties or unbounded JSON."""
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError
            result[name] = value
        return result
    try:
        if type(raw) is not bytes or len(raw) > _MAX_PAYLOAD:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=unique)
        _encode(value)
        return value
    except Exception:
        raise ConfigurationError('configuration_edit_invalid') from None


def _pretty(value):
    _encode(value)
    result = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
    if len(result) > _MAX_PAYLOAD:
        raise ConfigurationError('configuration_limit')
    return result


def _directory(directory, runtime):
    if (not isinstance(directory, Path) or not directory.is_absolute() or '..' in directory.parts
            or directory.is_relative_to(runtime.store.root) or runtime.store.root.is_relative_to(directory)):
        raise ConfigurationError('configuration_edit_invalid')


def export_edit(runtime, directory: Path, *, binding_digest, defaults):
    """Create a new private working set; never replace an existing edit."""
    if not _digest(binding_digest) or runtime.dirty or runtime.uncertain is not None:
        raise ConfigurationError('configuration_conflict')
    _directory(directory, runtime)
    snapshot = runtime.reload()
    values = defaults if snapshot is None else snapshot.record['values']
    staged = {}
    for name in runtime.validators:
        if name not in values:
            raise ConfigurationError('configuration_incomplete')
        raw = _encode(values[name])
        runtime._validate(name, raw)
        staged[name] = _pretty(values[name])
    manifest = dict(schema=_SCHEMA, version=1, store_id=runtime.store.store_id,
        binding=binding_digest, base=None if snapshot is None else snapshot.digest,
        base_revision=0 if snapshot is None else snapshot.revision,
        owners=sorted(runtime.validators))
    runtime.store.authority.check()
    ensure_private_directory(directory, create=True)
    if next(directory.iterdir(), None) is not None:
        raise ConfigurationError('configuration_edit_exists')
    ensure_private_directory(directory/'owners', create=True)
    for name, raw in staged.items():
        runtime.store.authority.check()
        write_private_file(directory/'owners'/(name+'.json'), raw, replace=False)
    runtime.store.authority.check()
    write_private_file(directory/'cadence.json', _pretty({'seconds': runtime.cadence}), replace=False)
    # The manifest is last: incomplete exports cannot become flushable sets.
    runtime.store.authority.check()
    write_private_file(directory/'manifest.json', _pretty(manifest), replace=False)
    return directory


def _edit_operation(directory, manifest_bytes, record):
    return hashlib.sha256(_encode({'domain': _SCHEMA, 'directory': str(directory),
        'manifest': hashlib.sha256(manifest_bytes).hexdigest(), 'values': record['values'],
        'cadence': record['cadence_seconds'], 'extensions': record['extensions']})).hexdigest()


def flush_edit(runtime, directory: Path, *, binding_digest, authorized):
    """Flush one stable complete working set without modifying editable files.

    The signed operation digest lets later edits reuse this same working set.
    A revision written through another set or owner remains an explicit conflict.
    """
    if (authorized is not True or not _digest(binding_digest)
            or runtime.dirty or runtime.uncertain is not None):
        raise ConfigurationError('configuration_unauthorized')
    _directory(directory, runtime)
    ensure_private_directory(directory, create=False)
    with ExitStack() as stack:
        held = []
        def read(path):
            try:
                source = stack.enter_context(AnchoredFile(path, private=True))
            except FileNotFoundError:
                raise ConfigurationError('configuration_edit_incomplete') from None
            except (OSError, EnvelopeError):
                raise ConfigurationError('configuration_edit_invalid') from None
            held.append(source)
            if os.fstat(source.file).st_nlink != 1 or not 1 <= source.size <= _MAX_PAYLOAD:
                raise ConfigurationError('configuration_edit_invalid')
            raw = bytearray()
            while len(raw) < source.size:
                chunk = os.read(source.file, min(65536, source.size-len(raw)))
                if not chunk:
                    raise ConfigurationError('configuration_edit_changed')
                raw.extend(chunk)
            if not source.unchanged():
                raise ConfigurationError('configuration_edit_changed')
            return bytes(raw)
        manifest_bytes = read(directory/'manifest.json')
        manifest = editable_json(manifest_bytes)
        if (type(manifest) is not dict
                or set(manifest) != {'schema', 'version', 'store_id', 'binding', 'base', 'base_revision', 'owners'}
                or manifest['schema'] != _SCHEMA or type(manifest['version']) is not int or manifest['version'] != 1
                or manifest['store_id'] != runtime.store.store_id or manifest['binding'] != binding_digest
                or manifest['owners'] != sorted(runtime.validators)
                or type(manifest['base_revision']) is not int or manifest['base_revision'] < 0
                or (manifest['base_revision'] == 0) != (manifest['base'] is None)
                or manifest['base'] is not None and not _digest(manifest['base'])):
            raise ConfigurationError('configuration_edit_invalid')
        cadence = editable_json(read(directory/'cadence.json'))
        if type(cadence) is not dict or set(cadence) != {'seconds'}:
            raise ConfigurationError('configuration_edit_invalid')
        values = {name: editable_json(read(directory/'owners'/(name+'.json')))
                  for name in runtime.validators}
        original = runtime.store.authority
        def stable():
            return all(source.unchanged() for source in held)
        def check():
            original.check()
            if not stable():
                raise ConfigurationError('configuration_edit_changed')
        authority = replace(original, current_check=check)
        store = ConfigurationStore(runtime.store.root, runtime.store.store_id, authority)
        active = ConfigurationRuntime(store, runtime.validators, **runtime.provenance)
        current = active.accepted
        current_digest = None if current is None else current.digest
        if manifest['base'] != current_digest:
            if (current is None or current.record['provenance'] is None
                    or current.record['provenance']['operation'] != _edit_operation(directory, manifest_bytes, current.record)):
                raise ConfigurationError('configuration_conflict')
            store.load(minimum_revision=manifest['base_revision'], ancestor_digest=manifest['base'])
        elif (0 if current is None else current.revision) != manifest['base_revision']:
            raise ConfigurationError('configuration_conflict')
        planned = {} if current is None else current.record['values']
        planned.update(values)
        for name, value in values.items():
            active.stage(name, value)
        active.stage_cadence(cadence['seconds'])
        proposed = dict(values=planned, cadence_seconds=cadence['seconds'],
                        extensions={} if current is None else current.record['extensions'])
        active.provenance['operation'] = _edit_operation(directory, manifest_bytes, proposed)
        try:
            check()
            if (current is not None and proposed['values'] == current.record['values']
                    and proposed['cadence_seconds'] == current.record['cadence_seconds']):
                runtime.reload()
                return current
            result = active.flush(authorized=authorized)
            try:
                check()
                runtime.reload()
            except Exception:
                raise ConfigurationError('configuration_uncertain', candidate=result.digest) from None
            return result
        except ConfigurationError as error:
            if error.code != 'configuration_uncertain' and not stable():
                raise ConfigurationError('configuration_edit_changed', candidate=error.candidate) from None
            raise

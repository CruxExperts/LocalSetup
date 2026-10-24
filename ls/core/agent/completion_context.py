"""Bounded, explicitly disclosed context for tool-free completion."""
import hashlib
from pathlib import Path
import tempfile
import uuid

from .broker_rpc import _decode, _encode
from .completion_contract import MAX_REQUEST
from .file_grants import FileGrant, relative

MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_PAGES = 128


def validate_paths(paths):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 8
            or any(not isinstance(path, str) or len(path) > 1024 for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError('Completion context requires one to eight distinct bounded paths')
    for path in paths:
        relative(path)


def attach(raw, paths, grant_path, workspace, expires, cancelled):
    """Read snapshots through the shared broker; never grant tools or writes."""
    from .run_cli import _grant
    from .file_broker import FileBroker
    validate_paths(paths)
    request = _decode(raw.encode('utf-8'))
    if not isinstance(request, dict) or 'input' not in request:
        raise ValueError('Completion context requires a request input')
    workspace = Path(workspace).absolute()
    value, _ = _grant(Path(grant_path).absolute(), workspace)
    task = uuid.uuid4().hex
    grant = FileGrant(task, task, workspace, tuple(value['read']), (),
                      tuple(value['disclose']), expires, revoked=cancelled)
    # Check every selection before reading any content.
    for path in paths:
        grant.check(task, task, 'read', path, provider=True)
    contexts, total, pages = [], 0, 0
    with tempfile.TemporaryDirectory(prefix='lscli-context-') as temporary:
        broker = FileBroker(grant, Path(temporary))
        try:
            for path in paths:
                parts, cursor = [], None
                while True:
                    if pages >= MAX_CONTEXT_PAGES:
                        raise ValueError('Completion context exceeds page limit')
                    page = broker.read_page(task, task, path, cursor, for_provider=True)
                    pages += 1
                    if page['encoding'] != 'utf-8':
                        raise ValueError('Completion context requires plain UTF-8 text')
                    total += len(page['content'].encode('utf-8'))
                    if total > MAX_CONTEXT_BYTES:
                        raise ValueError('Completion context exceeds byte limit')
                    parts.append(page['content'])
                    cursor = page['next_cursor']
                    if cursor is None:
                        break
                text = ''.join(parts)
                contexts.append({'path': path, 'text': text,
                                 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()})
            for path in paths:
                grant.check(task, task, 'read', path, provider=True)
        finally:
            broker.clear_page_cache()
    request['input'] = {'instructions': request['input'], 'context': contexts}
    result = _encode(request)
    if len(result) > MAX_REQUEST:
        raise ValueError('Completion request with context exceeds byte limit')
    return result.decode('utf-8')

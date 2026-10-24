"""Project only text leaves of known tool results; leave receipt fields intact."""
import json

from ..output_filters import FilterError
from .broker_rpc import _decode


def fields(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise FilterError('filter_context_unsupported')


def items(value):
    if type(value) is not list or len(value) > 1024:
        raise FilterError('filter_context_limit')
    return value


def tool_view(raw, project):
    if type(raw) is not str:
        raise FilterError('filter_context_unsupported')
    try:
        value = _decode(raw)
    except (ValueError, TypeError):
        # SDK retry messages are ordinary text, not serialized tool receipts.
        if raw.lstrip().startswith(('{', '[')):
            raise FilterError('filter_context_unsupported') from None
        return project.inline(raw)
    if type(value) is str:
        return json.dumps(project.inline(value), ensure_ascii=False)
    if type(value) is not dict:
        raise FilterError('filter_context_unsupported')
    start = len(project.decisions)
    keys = set(value)
    if keys == {'content', 'encoding', 'bytes', 'revision', 'next_cursor'}:
        if value['encoding'] != 'utf-8':
            raise FilterError('filter_context_unsupported')
        value['content'] = project.text(value['content'])
    elif keys == {'operation', 'status', 'returncode', 'output'}:
        if value['output'] is not None:
            fields(value['output'], ('stdout', 'stderr'))
            for name in ('stdout', 'stderr'):
                value['output'][name] = project.text(value['output'][name])
    elif keys == {'files', 'matches', 'truncated'}:
        for match in items(value['matches']):
            fields(match, ('path', 'line', 'text', 'text_truncated'))
            match['text'] = project.text(match['text'])
        for file in items(value['files']):
            fields(file, ('path', 'sha256'))
    elif keys == {'directory', 'resources', 'missing'}:
        for resource in items(value['resources']):
            fields(resource, ('path', 'content', 'sha256'))
            resource['content'] = project.text(resource['content'])
    elif keys == {'path', 'entries', 'truncated'}:
        for entry in items(value['entries']):
            fields(entry, ('path', 'kind'))
    elif keys != {'operation', 'status'}:
        raise FilterError('filter_context_unsupported')
    changes = project.decisions[start:]
    if changes:
        value['_localsetup_filters'] = changes
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

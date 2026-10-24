"""Bounded provider-only output views; original history and input stay unchanged."""
import base64
import json
import time

from ..output_filters import FilterError
from .broker_rpc import _decode, _encode
from .context_tool_views import fields, items, tool_view

MAX_BODY = 10 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_LINES = 20000
MAX_UNITS = 256
MAX_WORK = 16 * 1024 * 1024


class ContextFilter:
    def __init__(self, policy, check, cancelled, authority_check, *, refresh=lambda: None):
        self.policy, self.current, self.cancelled, self.authority = policy, check, cancelled, authority_check
        self.refresh = refresh
        self.work = 0

    def retained(self, events):
        """Apply the current policy to retained views, never to stored records."""
        self.refresh()
        self.current()
        self.authority()
        projection = _Projection(self)
        result = []
        for event in items(events):
            projection.check()
            view = None if event['text'] is None else projection.result(event['text'])
            result.append(dict(event, text=None if view is None else view.text,
                               context_filter=None if view is None else view.metadata()))
        self.authority()
        projection.check()
        return result

    def __call__(self, data, api):
        self.refresh()
        self.current()
        self.authority()
        if not any('agent-context' in rule.sinks for rule in self.policy.rules):
            return data
        projection = _Projection(self)
        projection.check()
        fields(data, ('url', 'method', 'body'))
        if type(data['body']) is not str or len(data['body']) > MAX_BODY * 4 // 3 + 4:
            raise FilterError('filter_context_limit')
        try:
            raw = base64.b64decode(data['body'], validate=True)
            if len(raw) > MAX_BODY:
                raise FilterError('filter_context_limit')
            packet = _decode(raw)
        except (ValueError, TypeError, RecursionError):
            raise FilterError('filter_context_unsupported') from None
        if type(packet) is not dict:
            raise FilterError('filter_context_unsupported')
        # Server-side history is opaque to this mandatory output filter.
        if packet.get('previous_response_id') or packet.get('conversation'):
            raise FilterError('filter_context_unsupported')
        projection.check()
        if api == 'chat_completions':
            projection.chat(items(packet.get('messages')))
        elif api == 'responses':
            value = packet.get('input')
            if type(value) is not str:
                projection.responses(items(value))
        else:
            raise FilterError('filter_context_unsupported')
        projection.check()
        encoded = _encode(packet)
        if len(encoded) > MAX_BODY:
            raise FilterError('filter_context_limit')
        self.authority()
        projection.check()
        return dict(data, body=base64.b64encode(encoded).decode('ascii'))


class _Projection:
    def __init__(self, owner):
        self.owner = owner
        self.policy = owner.policy
        self.deadline = time.monotonic() + 1
        self.size, self.lines, self.units = 0, 0, 0
        self.decisions = []

    def check(self):
        self.owner.current()
        if self.owner.cancelled.is_set():
            raise InterruptedError('Context filter requested cancellation')
        if time.monotonic() >= self.deadline:
            raise FilterError('filter_context_timeout')

    def result(self, value):
        self.check()
        if type(value) is not str:
            raise FilterError('filter_context_unsupported')
        size, lines = len(value.encode()), value.count('\n') + 1
        self.size += size
        self.lines += lines
        self.units += 1
        self.owner.work += size
        if self.size > MAX_TEXT or self.lines > MAX_LINES or self.units > MAX_UNITS or self.owner.work > MAX_WORK:
            raise FilterError('filter_context_limit')
        result = self.policy.apply(value, sink='agent-context', check=self.check)
        if result.action == 'halt':
            self.owner.cancelled.set()
            raise InterruptedError('Context filter requested cancellation')
        rendered = result.text if result.text is not None else '[Output withheld by filter.]'
        self.size += max(0, len(rendered.encode()) - size)
        self.lines += max(0, rendered.count('\n') + 1 - lines)
        if self.size > MAX_TEXT or self.lines > MAX_LINES:
            raise FilterError('filter_context_limit')
        return result

    def text(self, value):
        result = self.result(value)
        if result.action != 'allow':
            self.decisions.append(dict(unit=self.units, **result.metadata()))
        return result.text if result.text is not None else '[Output withheld by filter.]'

    def inline(self, value):
        before = len(self.decisions)
        text = self.text(value)
        if len(self.decisions) != before:
            marker = '\n[LocalSetup context filter: ' + json.dumps(self.decisions[-1], separators=(',', ':')) + ']'
            self.size += len(marker.encode())
            self.lines += 1
            if self.size > MAX_TEXT or self.lines > MAX_LINES:
                raise FilterError('filter_context_limit')
            text += marker
        return text

    def content(self, value):
        if type(value) is str:
            return self.inline(value)
        if value is None:
            return None
        for part in items(value):
            if type(part) is not dict or part.get('type') not in ('text', 'output_text', 'refusal'):
                raise FilterError('filter_context_unsupported')
            key = 'refusal' if part['type'] == 'refusal' else 'text'
            fields(part, ('type', key), ('annotations', 'logprobs'))
            # Annotations and logprobs can themselves contain output text. The
            # current SDK emits empty annotations for replayed text parts.
            if part.get('annotations') or part.get('logprobs'):
                raise FilterError('filter_context_unsupported')
            part[key] = self.inline(part[key])
        return value

    def chat(self, messages):
        for message in messages:
            self.check()
            if type(message) is not dict:
                raise FilterError('filter_context_unsupported')
            role = message.get('role')
            if role in ('system', 'developer', 'user'):
                continue  # Input semantics, including images, are not output views.
            if role == 'assistant':
                fields(message, ('role',), ('content', 'tool_calls', 'name', 'refusal', 'reasoning', 'reasoning_content'))
                for call in items(message.get('tool_calls', [])):
                    fields(call, ('id', 'type', 'function'))
                    fields(call['function'], ('name', 'arguments'))
                    if call['type'] != 'function':
                        raise FilterError('filter_context_unsupported')
                if 'content' in message:
                    message['content'] = self.content(message['content'])
                for key in ('refusal', 'reasoning', 'reasoning_content'):
                    if message.get(key) is not None:
                        message[key] = self.inline(message[key])
            elif role == 'tool':
                fields(message, ('role', 'content', 'tool_call_id'), ('name',))
                message['content'] = tool_view(message['content'], self)
            else:
                raise FilterError('filter_context_unsupported')

    def responses(self, messages):
        for message in messages:
            self.check()
            if type(message) is not dict:
                raise FilterError('filter_context_unsupported')
            kind = message.get('type', 'message')
            if kind == 'message':
                if message.get('role') in ('system', 'developer', 'user'):
                    continue
                fields(message, ('role', 'content'), ('type', 'id', 'status', 'phase'))
                if message['role'] != 'assistant':
                    raise FilterError('filter_context_unsupported')
                message['content'] = self.content(message['content'])
            elif kind == 'function_call':
                fields(message, ('type', 'call_id', 'name', 'arguments'), ('id', 'status'))
            elif kind == 'function_call_output':
                fields(message, ('type', 'call_id', 'output'), ('id', 'status'))
                message['output'] = tool_view(message['output'], self)
            elif kind == 'reasoning':
                fields(message, ('type',), ('id', 'summary', 'content', 'status', 'encrypted_content'))
                if message.get('encrypted_content'):
                    raise FilterError('filter_context_unsupported')
                for key, expected in (('summary', 'summary_text'), ('content', 'reasoning_text')):
                    for part in items(message.get(key, [])):
                        fields(part, ('type', 'text'))
                        if part['type'] != expected:
                            raise FilterError('filter_context_unsupported')
                        part['text'] = self.inline(part['text'])
            else:
                raise FilterError('filter_context_unsupported')

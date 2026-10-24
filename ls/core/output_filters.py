"""Ordered, bounded text-view filtering; no grants, file writes or trust changes.

Consumers select a sink and supply complete text units. They must withhold
blocked/quarantined text and route halt through their existing cancellation owner.
Never run this transformation over authenticated records or protocol envelopes.
"""
from dataclasses import dataclass, field
import hashlib
import json
import time


SINKS = frozenset({'observation', 'retained-log', 'agent-context'})
DEFAULT_LIMITS = {'max_text_bytes': 65536, 'max_matches': 1024, 'timeout_ms': 100}


class FilterError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def defaults():
    return {'schema_version': 1, 'rules': [], 'limits': dict(DEFAULT_LIMITS)}


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or not empty and not value):
        raise FilterError('filter_invalid')
    try:
        if len(value.encode('utf-8')) > maximum:
            raise FilterError('filter_invalid')
    except UnicodeError:
        raise FilterError('filter_invalid') from None
    return value


def _pattern(pattern):
    """Bound compilation syntax as well as match execution.

    Basic regex groups, alternation, classes and repetition are supported.
    Extensions, backreferences, recursion and large counted repeats are not.
    """
    depth, in_class, index = 0, False, 0
    while index < len(pattern):
        char = pattern[index]
        if char == '\\':
            index += 1
            if index == len(pattern) or pattern[index] not in 'AbBdDsSwWZtnrfv\\.^$*+?{}[]()|-':
                raise FilterError('filter_pattern_unsupported')
        elif in_class:
            if char == ']':
                in_class = False
        elif char == '[':
            in_class = True
        elif char == '(':
            depth += 1
            if depth > 8 or pattern[index:index+2] in ('(?', '(*'):
                raise FilterError('filter_pattern_unsupported')
        elif char == ')':
            depth -= 1
        elif char == '{':
            end = pattern.find('}', index+1)
            parts = pattern[index+1:end].split(',') if end >= 0 else []
            if (not 1 <= len(parts) <= 2 or any(not item.isascii() or not item.isdigit()
                    or len(item) > 3 or int(item) > 128 for item in parts)):
                raise FilterError('filter_pattern_unsupported')
            index = end
        index += 1


@dataclass(frozen=True, slots=True)
class _Rule:
    order: int
    sinks: tuple[str, ...]
    action: str
    replacement: str
    compiled: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FilterResult:
    text: str | None = field(repr=False)
    action: str
    orders: tuple[int, ...]
    matches: int
    policy_sha256: str

    def metadata(self):
        return dict(schema_version=1, action=self.action, orders=list(self.orders),
                    matches=self.matches, policy_sha256=self.policy_sha256)


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    sha256: str
    max_text_bytes: int
    max_matches: int
    timeout_ms: int
    rules: tuple[_Rule, ...] = field(repr=False)

    def apply(self, text, *, sink, check=lambda: None):
        if type(sink) is not str or sink not in SINKS:
            raise FilterError('filter_sink_invalid')
        if not callable(check):
            raise FilterError('filter_invalid')
        check()
        _text(text, self.max_text_bytes, empty=True)
        deadline = time.monotonic() + self.timeout_ms/1000
        matched, orders = 0, []
        for rule in self.rules:
            check()
            if rule.sinks and sink not in rule.sinks:
                continue
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise FilterError('filter_timeout')
            pieces, offset, size, found = [], 0, 0, False
            try:
                # One regex call may occupy at most 20ms before cancellation
                # is rechecked. Expiry is a failure, never a pass-through.
                for match in rule.compiled.finditer(text, timeout=min(.02, remaining)):
                    check()
                    if time.monotonic() >= deadline:
                        raise FilterError('filter_timeout')
                    matched += 1
                    if matched > self.max_matches:
                        raise FilterError('filter_limit')
                    if not found:
                        orders.append(rule.order)
                        found = True
                    if rule.action != 'replace':
                        return FilterResult(None, rule.action, tuple(orders), matched, self.sha256)
                    piece = text[offset:match.start()] + rule.replacement
                    size += len(piece.encode('utf-8'))
                    if size > self.max_text_bytes:
                        raise FilterError('filter_limit')
                    pieces.append(piece)
                    offset = match.end()
            except TimeoutError:
                check()
                raise FilterError('filter_timeout') from None
            check()
            if time.monotonic() >= deadline:
                raise FilterError('filter_timeout')
            if found:
                tail = text[offset:]
                if size + len(tail.encode('utf-8')) > self.max_text_bytes:
                    raise FilterError('filter_limit')
                text = ''.join(pieces) + tail
        check()
        return FilterResult(text, 'transform' if orders else 'allow', tuple(orders), matched, self.sha256)


def _rule(value, engine):
    fields = {'order', 'sinks', 'match', 'pattern', 'action'}
    if (type(value) is not dict or set(value) != fields | ({'replacement'} if value.get('action') == 'replace' else set())
            or type(value['order']) is not int or not 1 <= value['order'] <= 2**31-1
            or type(value['sinks']) is not list or not 1 <= len(value['sinks']) <= len(SINKS)
            or any(type(sink) is not str or sink not in SINKS for sink in value['sinks'])
            or len(set(value['sinks'])) != len(value['sinks'])
            or value['match'] not in ('word', 'regex')
            or value['action'] not in ('replace', 'block', 'quarantine', 'halt')):
        raise FilterError('filter_invalid')
    pattern = _text(value['pattern'], 256)
    replacement = _text(value.get('replacement', ''), 4096, empty=True)
    if value['match'] == 'word':
        if any(not (char.isalnum() or char == '_') for char in pattern):
            raise FilterError('filter_invalid')
        pattern = r'\b' + engine.escape(pattern) + r'\b'
    else:
        _pattern(pattern)
    try:
        compiled = engine.compile(pattern, engine.VERSION0)
    except (engine.error, OverflowError, RecursionError):
        raise FilterError('filter_invalid') from None
    return _Rule(value['order'], tuple(sorted(value['sinks'])), value['action'], replacement, compiled)


def parse_policy(value):
    if (type(value) is not dict or set(value) != {'schema_version', 'rules', 'limits'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['rules']) is not list or len(value['rules']) > 64
            or type(value['limits']) is not dict or set(value['limits']) != set(DEFAULT_LIMITS)):
        raise FilterError('filter_invalid')
    limits = value['limits']
    for name, maximum in (('max_text_bytes', 1024*1024), ('max_matches', 4096), ('timeout_ms', 1000)):
        if type(limits[name]) is not int or not 1 <= limits[name] <= maximum:
            raise FilterError('filter_invalid')
    try:
        import regex
    except ImportError:
        raise FilterError('filter_engine_unavailable') from None
    rules = tuple(sorted((_rule(item, regex) for item in value['rules']), key=lambda rule: rule.order))
    if len({rule.order for rule in rules}) != len(rules):
        raise FilterError('filter_duplicate_order')
    # The input contains only validated bounded primitives; keep no mutable
    # caller-owned configuration and expose no pattern or input in metadata.
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=True).encode()).hexdigest()
    return FilterPolicy(digest, **limits, rules=rules)


def validate_policy(value):
    parse_policy(value)

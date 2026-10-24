"""Deterministic text transformations with bounded failure and no raw receipts."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from ls.core.output_filters import FilterError, defaults, parse_policy
from ls.core.schema import validate_json_schema


def rule(order=10, *, match='word', pattern='secret', action='replace', replacement='redacted',
         sinks=('observation', 'retained-log', 'agent-context')):
    value = dict(order=order, sinks=list(sinks), match=match, pattern=pattern, action=action)
    if action == 'replace':
        value['replacement'] = replacement
    return value


def policy(*rules, **limits):
    value = defaults()
    value['rules'] = list(rules)
    value['limits'].update(limits)
    return value


def test_order_continues_transforms_and_applies_same_policy_to_each_sink():
    value = policy(rule(30, pattern='redacted', replacement='hidden'), rule(10))
    parsed = parse_policy(value)
    value['rules'][1]['replacement'] = 'mutated'
    for sink in ('observation', 'retained-log', 'agent-context'):
        result = parsed.apply('secret secretary secret', sink=sink)
        assert result.text == 'hidden secretary hidden'
        assert result.orders == (10, 30) and result.matches == 4 and result.action == 'transform'
        assert result.metadata()['policy_sha256'] == parsed.sha256
        assert 'secret' not in json.dumps(result.metadata()) and 'hidden' not in repr(result)
    selected = parse_policy(policy(rule(sinks=('agent-context',))))
    assert selected.apply('secret', sink='observation').text == 'secret'
    assert selected.apply('secret', sink='agent-context').text == 'redacted'


@pytest.mark.parametrize('action', ['block', 'quarantine', 'halt'])
def test_terminal_actions_withhold_content_and_do_not_run_later_rules(action):
    parsed = parse_policy(policy(rule(), rule(20, pattern='redacted', action=action),
                                 rule(30, pattern='redacted', replacement='not reached')))
    result = parsed.apply('secret', sink='retained-log')
    assert result.text is None and result.action == action and result.orders == (10, 20)


def test_regex_replacement_is_literal_unicode_bounded_and_zero_width_safe():
    parsed = parse_policy(policy(rule(match='regex', pattern=r'TOKEN=\w+', replacement=r'\1')))
    assert parsed.apply('TOKEN=private', sink='observation').text == r'\1'
    assert parse_policy(policy(rule(pattern='café'))).apply('café', sink='observation').text == 'redacted'
    with pytest.raises(FilterError, match='filter_limit'):
        parse_policy(policy(rule(pattern='x', replacement='😀'), max_text_bytes=3)).apply('x', sink='observation')
    with pytest.raises(FilterError, match='filter_limit'):
        parse_policy(policy(rule(match='regex', pattern='^|$'), max_matches=1)).apply('x', sink='observation')


@pytest.mark.parametrize('pattern', [r'(a)\1', r'(?R)', r'(*SKIP)', r'a{999}', r'a{1,}', '(' * 9+'x'+')' * 9])
def test_unsupported_regex_is_rejected_before_execution(pattern):
    with pytest.raises(FilterError, match='filter_pattern_unsupported'):
        parse_policy(policy(rule(match='regex', pattern=pattern)))


def test_pathological_regex_and_cancellation_fail_closed():
    parsed = parse_policy(policy(rule(match='regex', pattern='(a|aa)+$', action='block'), timeout_ms=1))
    start = time.monotonic()
    with pytest.raises(FilterError, match='filter_timeout'):
        parsed.apply('a'*30000+'!', sink='observation')
    assert time.monotonic()-start < 1
    def cancelled():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        parsed.apply('secret', sink='observation', check=cancelled)


def test_schema_and_semantic_policy_reject_invalid_authority_inputs():
    schema = Path(__file__).resolve().parents[1]/'config/output-filter.schema.json'
    value = policy(rule())
    assert not validate_json_schema(value, schema, label='filters')
    mutations = [dict(value, schema_version=2), dict(value, rules=[rule(order=True)]),
                 dict(value, rules=[rule(action='unknown')]), dict(value, rules=[rule(sinks=('unknown',))])]
    for invalid in mutations:
        assert validate_json_schema(invalid, schema, label='filters')
        with pytest.raises(FilterError):
            parse_policy(invalid)
    with pytest.raises(FilterError, match='filter_duplicate_order'):
        parse_policy(policy(rule(), rule()))
    invalid = deepcopy(value)
    invalid['rules'][0]['pattern'] = '['
    invalid['rules'][0]['match'] = 'regex'
    with pytest.raises(FilterError, match='^filter_invalid$'):
        parse_policy(invalid)
    with pytest.raises(FilterError):
        parse_policy(value).apply('x'*65537, sink='observation')

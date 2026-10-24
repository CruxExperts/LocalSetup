"""Signed live filter revisions at whole-delivery boundaries."""
import base64
from copy import deepcopy
import json
import threading

import pytest

from ls.core.agent import output_views as views
from ls.core.agent import context_filters
from ls.core.live_configuration import ConfigurationError
from ls.core.openpgp.container_storage import write_private_file
from ls.core.output_filters import FilterError, parse_policy
from ls.tests.test_output_views import signed_filter
from ls.tests.test_openpgp_artifacts import separate_machine_env


def consumer(tmp_path, selected, monkeypatch, *, retain=False):
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    environment = views.launch_environment(selected, workspace, retain=retain)
    with monkeypatch.context() as launch:
        launch.setattr(views.os, 'environ', {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **environment})
        view = views.protected(selected, workspace, lambda: None, threading.Event(), retain=retain)
    now = [0]
    view.runtime.clock = lambda: now[0]
    view.runtime.next_due = 60
    return view, now


def request():
    packet = dict(messages=[dict(role='user', content='secret'),
                            dict(role='assistant', content='secret')])
    return dict(url='https://example.invalid/v1/', method='POST',
                body=base64.b64encode(json.dumps(packet).encode()).decode())


def test_signed_cadence_updates_all_sinks_and_enables_context_without_resetting_work(
        tmp_path, signed_filter, monkeypatch):
    selected, writer, policy = signed_filter
    view, now = consumer(tmp_path, selected, monkeypatch, retain=True)
    saved = []
    class Buffer:
        def append(self, value, **_metadata):
            saved.append(value)
    view.retention = Buffer()
    data = request()
    assert view.context(data, 'chat_completions') is data
    policy['rules'][0].update(sinks=['observation', 'retained-log', 'agent-context'], replacement='updated')
    writer.stage('filters', policy)
    writer.stage_cadence(2)
    writer.flush(authorized=True)
    now[0] = 59
    assert view.text('secret')['text'] == 'hidden'
    assert saved[-1]['text'] == 'secret'
    now[0] = 60
    observed = view.text('secret')
    assert observed['text'] == saved[-1]['text'] == 'updated'
    assert observed['filter'] == saved[-1]['filter']
    assert view.runtime.next_due == 62
    projected = json.loads(base64.b64decode(view.context(data, 'chat_completions')['body']))
    assert projected['messages'][0]['content'] == 'secret'
    assert projected['messages'][1]['content'].startswith('updated\n[LocalSetup context filter: ')
    work = view.context_views.work
    assert work == len('secret')
    policy['rules'][0]['replacement'] = 'latest'
    writer.stage('filters', policy); writer.flush(authorized=True)
    now[0] = 62
    monkeypatch.setattr(context_filters, 'MAX_WORK', work)
    with pytest.raises(FilterError, match='filter_context_limit'):
        view.context(data, 'chat_completions')
    assert view.applied == writer.accepted.digest
    assert view.context_views.work > work


@pytest.mark.parametrize('failure', ['sink', 'ciphertext', 'rollback'])
def test_failed_reload_keeps_last_policy_but_closes_delivery(tmp_path, signed_filter, monkeypatch, failure):
    selected, writer, policy = signed_filter
    view, now = consumer(tmp_path, selected, monkeypatch)
    original = view.policy
    initial_head = (writer.store.root/'HEAD').read_bytes()
    policy['rules'][0]['replacement'] = 'updated'
    if failure == 'sink':
        policy['rules'][0]['sinks'].append('retained-log')
    writer.stage('filters', policy); writer.flush(authorized=True)
    if failure == 'ciphertext':
        write_private_file(writer.store.root/'objects'/writer.accepted.digest, b'invalid', replace=True)
    if failure == 'rollback':
        now[0] = 60
        assert view.text('secret')['text'] == 'updated'
        original = view.policy
        write_private_file(writer.store.root/'HEAD', initial_head, replace=True)
    now[0] = view.runtime.next_due
    with pytest.raises(ConfigurationError):
        view.text('secret')
    assert view.policy is original
    assert view.runtime.accepted.digest == view.applied
    # The shared cadence advanced after failure; no early delivery may bypass it.
    with pytest.raises(ConfigurationError):
        view.text('secret')
    with pytest.raises(ConfigurationError):
        view.context(request(), 'chat_completions')


def test_one_policy_for_both_sinks_even_if_owner_changes_during_checks():
    from ls.core.output_filters import defaults
    policy = defaults()
    policy['rules'] = [dict(order=10, sinks=['observation', 'retained-log', 'agent-context'],
                            match='word', pattern='secret', action='replace', replacement='first')]
    first = parse_policy(policy)
    policy['rules'][0]['replacement'] = 'second'
    second = parse_policy(policy)
    view = views.Observations(first, lambda: None, threading.Event())
    saved = []
    class Buffer:
        def append(self, value, **_metadata): saved.append(value)
    view.retention = Buffer()
    original_apply = type(first).apply
    def replace_owner(self, *args, **kwargs):
        view.policy = second
        return original_apply(self, *args, **kwargs)
    from unittest.mock import patch
    with patch.object(type(first), 'apply', replace_owner):
        result = view.text('secret')
    assert result == saved[-1]
    assert result['text'] == 'first'


def test_projection_pins_all_units_and_cancellation_survives_reload(tmp_path, signed_filter, monkeypatch):
    selected, writer, policy = signed_filter
    policy['rules'][0]['sinks'] = ['agent-context']
    writer.stage('filters', policy); writer.flush(authorized=True)
    view, now = consumer(tmp_path, selected, monkeypatch)
    original = view.policy
    replacement = deepcopy(policy)
    replacement['rules'][0]['replacement'] = 'later'
    later = parse_policy(replacement)
    original_apply = type(original).apply
    def replace_owner(self, *args, **kwargs):
        view.context_views.policy = later
        return original_apply(self, *args, **kwargs)
    monkeypatch.setattr(type(original), 'apply', replace_owner)
    result = view.context_views.retained([dict(text='secret'), dict(text='secret')])
    assert [item['text'] for item in result] == ['hidden', 'hidden']
    assert result[0]['context_filter'] == result[1]['context_filter']
    view.cancelled.set()
    now[0] = 60
    with pytest.raises(InterruptedError):
        view.context(request(), 'chat_completions')

"""UI presentation resolves safe themes and keeps persistence authority explicit."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ls.core.application_profiles.service import load_tmux_runtime_defaults
from ls.core.live_configuration import ConfigurationError
from ls.core.live_configuration_cli import _owners
from ls.core.terminal_ops.command_backend import (
    TmuxCommandBackend, native_pane_border_format, native_status_options,
)
from ls.core.terminal_ops.command_state import ShellCommandError
from ls.core.terminal_ops.command_theme import (
    LEGACY_STOCK_PALETTE, _contrast, palette, preview, theme_definition, themes, validate_theme,
)

ROOT = Path(__file__).resolve().parents[2]


def test_startup_and_interaction_defaults():
    settings = load_tmux_runtime_defaults(source_root=ROOT)['settings']
    assert settings['startup_mode'] == 'stable'
    assert settings['inspection_enabled'] and settings['inspection_duration_seconds'] == 15
    assert settings['reflow_debounce_ms'] == 100 and settings['double_escape_ms'] == 500
    assert settings['auto_close_completed'] and settings['auto_close_delay_seconds'] == 30


@pytest.mark.parametrize('theme', themes(), ids=lambda value: value['name'])
def test_builtin_themes_share_renderer_and_accessible_text(theme):
    validate_theme(theme)
    colors = palette({'runtime_settings': {'theme': theme}})
    assert colors == theme['roles']
    for fg, bg in [('text', 'background'), ('text', 'surface'), ('text', 'toolbar'),
                   ('muted', 'background'), ('control_text', 'control'),
                   ('selection_text', 'selected'), ('selection_text', 'attention'),
                   ('success', 'background'), ('error', 'background'), ('running', 'background')]:
        assert _contrast(colors[fg], colors[bg]) >= 4.5
    assert '\x1b' not in preview(theme, color=False)
    assert '\x1b' in preview(theme)


def test_old_stock_and_custom_legacy_palettes_are_distinct():
    assert palette({'runtime_settings': {'theme': LEGACY_STOCK_PALETTE}}) == palette({})
    custom = LEGACY_STOCK_PALETTE | {'toolbar': '#182A3A'}
    assert palette({'runtime_settings': {'theme': custom}})['toolbar'] == '#182A3A'


def test_signed_owner_accepts_custom_definition_and_rejects_low_contrast(tmp_path):
    defaults, validators = _owners({'terminal': {'kind': 'terminal-runtime'}}, ROOT, tmp_path)
    candidate = deepcopy(defaults['terminal'])
    candidate['theme'] = theme_definition('graphite')
    validators['terminal'](candidate)
    candidate['theme']['roles']['text'] = candidate['theme']['roles']['toolbar']
    with pytest.raises(ConfigurationError, match='configuration_invalid'):
        validators['terminal'](candidate)


def test_native_status_keeps_actual_identity_and_shortens_location_first(monkeypatch):
    monkeypatch.setattr('ls.core.terminal_ops.command_backend.framework_version', lambda: '9.8.7')
    record = {'native_name': 'shell-1', 'native_session_id': '$2', 'canvas_width': 80}
    status = native_status_options(record)
    assert status['status'] == 'on' and status['status-position'] == 'top'
    assert '#{session_id} #{session_name}' in status['status-format[0]']
    assert status['@localsetup_version'] == '9.8.7'
    assert 'v#{@localsetup_version}' in status['status-format[0]']
    assert '#[fg=#FFFFFF]LS' in status['status-format[0]']
    assert 'session_path}' in status['status-format[0]']
    assert status['status-format[1]'] == ''
    assert '\x1b' not in str(status)


def test_signed_pre_ui_settings_remain_readable(tmp_path):
    from ls.core.terminal_ops.command_theme import PRESENTATION_KEYS
    defaults, validators = _owners({'terminal': {'kind': 'terminal-runtime'}}, ROOT, tmp_path)
    old = deepcopy(defaults['terminal'])
    for key in PRESENTATION_KEYS - {'theme'}:
        old.pop(key)
    old['theme'] = LEGACY_STOCK_PALETTE
    old.pop('native_shortcuts', None)
    validators['terminal'](old)


def test_shortcuts_are_configurable_without_widening_input_routes(tmp_path):
    from ls.core.terminal_ops.command_native_backend import native_control_bindings
    defaults, validators = _owners({'terminal': {'kind': 'terminal-runtime'}}, ROOT, tmp_path)
    settings = deepcopy(defaults['terminal'])
    settings['native_shortcuts']['menu'] = 'F8'
    validators['terminal'](settings)
    record = {'session_record_path': '/private/session.json', 'native_layout': {'grouped_sections': True},
              'runtime_settings': settings}
    bindings = native_control_bindings(record)
    assert 'F8' in bindings and 'F6' not in bindings
    assert 'Escape' in bindings and 'MouseDown1Pane' in bindings
    settings['native_shortcuts']['menu'] = 'Any'
    with pytest.raises(ConfigurationError):
        validators['terminal'](settings)



def test_native_backend_adapters_preserve_exact_operation_arguments():
    calls = []
    backend = TmuxCommandBackend()
    backend._native_canvas_v3 = SimpleNamespace(
        update_title_states=lambda *args: calls.append(('titles', args)),
        detach_exact_client=lambda *args: calls.append(('detach', args)))
    record, states, inventory = {'handle': 'fixture'}, {'operation': 'Failed'}, {'generation': 2}
    backend.update_native_title_states(record, states, inventory)
    backend.detach_native_client(record, 123)
    assert calls == [('titles', (record, states, inventory)), ('detach', (record, 123))]


def test_semantic_titles_expand_rgb_colors_without_tmux_shortcut_interpretation(tmp_path):
    tmux = shutil.which('tmux')
    if tmux is None:
        pytest.skip('Native tmux is required to verify its format expansion')
    socket = str(tmp_path / 'title.sock')
    def run(*args):
        return subprocess.run([tmux, '-S', socket, *args], check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    record = {'layout_mode': 'canvas-v3', 'native_layout': {'grouped_sections': True}}
    label = 'subagent worker purpose /repository'
    try:
        run('-f', '/dev/null', 'new-session', '-d', '-s', 'title-fixture')
        for name, value in [('role', 'operation'), ('label', label)]:
            run('set-option', '-p', '-t', 'title-fixture:0.0', '@localsetup_shell_' + name, value)
        colors = palette(record)
        for state, role in [('Failed', 'error'), ('Completed', 'success'), ('Running', 'running'),
                            ('Input needed', 'attention'), ('Finishing', 'running'), ('Idle', 'muted')]:
            run('set-option', '-p', '-t', 'title-fixture:0.0', '@localsetup_shell_state', state)
            rendered = run('display-message', '-p', '-t', 'title-fixture:0.0', '-F',
                           native_pane_border_format(record))
            assert rendered == (f"#[fg={colors['text']}]" + label
                                + f" | #[fg={colors[role]}]" + state + f"#[fg={colors['text']}]")
        run('set-option', '-p', '-t', 'title-fixture:0.0', '@localsetup_shell_role', 'status')
        assert run('display-message', '-p', '-t', 'title-fixture:0.0', '-F',
                   native_pane_border_format(record)) == label
    finally:
        subprocess.run([tmux, '-S', socket, 'kill-server'], capture_output=True, timeout=5)


@pytest.mark.parametrize('activity,expected', [(None, None), ('12345', 12345)])
def test_client_activity_readback_preserves_legacy_rows(monkeypatch, activity, expected):
    backend = TmuxCommandBackend()
    commands = []
    row = '123\t/dev/pts/1\tread-only,ignore-size\tfixture'
    if activity is not None:
        row += '\t' + activity
    def checked(endpoint, args):
        commands.append(args)
        return row
    monkeypatch.setattr(backend, '_checked', checked)
    clients = backend._clients({'endpoint': 'fixture', 'native_session_id': '$0', 'native_name': 'fixture'})
    assert clients == [{'pid': 123, 'name': '/dev/pts/1', 'flags': {'read-only', 'ignore-size'},
                        'activity_at': expected}]
    assert '#{client_activity}' in commands[0][-1]


@pytest.mark.parametrize('activity', ['', '0', '-1', 'unknown'])
def test_client_activity_rejects_unqualified_native_timestamps(monkeypatch, activity):
    backend = TmuxCommandBackend()
    monkeypatch.setattr(backend, '_checked', lambda *args: '123\t/dev/pts/1\tignore-size\tfixture\t' + activity)
    with pytest.raises(ShellCommandError) as error:
        backend._clients({'endpoint': 'fixture', 'native_session_id': '$0', 'native_name': 'fixture'})
    assert error.value.code == 'observer_identity_unknown'


def test_status_detail_and_colors_apply_to_real_native_chrome(tmp_path, monkeypatch):
    from ls.core.terminal_ops.command_live_configuration import _theme_matches
    from ls.core.terminal_ops.command_theme import native_border_styles
    tmux = shutil.which('tmux')
    if tmux is None:
        pytest.skip('Native tmux is required to verify chrome application and readback')
    socket = str(tmp_path / 'chrome.sock')
    def run(*args):
        return subprocess.run([tmux, '-S', socket, *args], check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    backend = TmuxCommandBackend(tmux_binary=tmux)
    monkeypatch.setattr(backend, '_checked', lambda endpoint, args, **kwargs: run(*args))
    service = SimpleNamespace(backend=backend)
    label = 'subagent worker purpose /repository'
    try:
        session_id, window_id = run('-f', '/dev/null', 'new-session', '-d', '-s', 'chrome-fixture',
                                    '-P', '-F', '#{session_id}\t#{window_id}').split('\t')
        record = {'endpoint': socket, 'layout_mode': 'canvas-v3', 'native_session_id': session_id,
                  'native_name': 'chrome-fixture', 'status_window_id': window_id,
                  'native_layout': {'grouped_sections': True, 'mode': 'stable', 'page': 0}}
        for name, value in [('role', 'operation'), ('label', label), ('state', 'Failed')]:
            run('set-option', '-p', '-t', 'chrome-fixture:0.0', '@localsetup_shell_' + name, value)
        for name, value in [('view_mode', 'fixed'), ('page', '2'), ('active', '2'), ('done', '1')]:
            run('set-option', '-t', session_id, '@localsetup_shell_' + name, value)
        for detail in ('minimal', 'standard', 'detailed'):
            for color in (False, True):
                settings = {'status_detail': detail, 'colors_enabled': color}
                current = record | {'runtime_settings': settings}
                backend.apply_native_control_theme(current)
                _theme_matches(service, record, settings)
                header = run('display-message', '-p', '-t', 'chrome-fixture:0.0', '-F',
                             native_status_options(current)['status-format[0]'])
                title = run('display-message', '-p', '-t', 'chrome-fixture:0.0', '-F',
                            native_pane_border_format(current))
                assert session_id + ' chrome-fixture' in header
                assert label in title
                if detail == 'minimal':
                    assert ' | ' not in header and title == label
                else:
                    assert ' | ' in header and 'Failed' in title
                if detail == 'detailed':
                    assert 'fixed page 3 | active 2 done 1' in header
                else:
                    assert 'page ' not in header and 'active ' not in header
                if not color:
                    assert '#[' not in header + title
                    assert set(native_border_styles(current).values()) == {'default'}
                    assert run('show-options', '-t', session_id, '-qv', 'status-style') == 'default'
                run('set-window-option', '-t', window_id, 'pane-border-style', 'fg=red')
                with pytest.raises(ConfigurationError, match='configuration_native_pending'):
                    _theme_matches(service, record, settings)
    finally:
        subprocess.run([tmux, '-S', socket, 'kill-server'], capture_output=True, timeout=5)


def test_disabled_chrome_settings_preserve_legacy_native_presentation():
    from ls.core.terminal_ops.command_theme import native_border_styles
    settings = load_tmux_runtime_defaults(source_root=ROOT)['settings'] | {
        'colors_enabled': False, 'status_detail': 'minimal'}
    legacy = {'layout_mode': 'canvas-v3', 'native_layout': {}, 'runtime_settings': settings}
    assert '@localsetup_shell_state' in native_pane_border_format(legacy)
    assert '#[' in native_status_options(legacy)['status-format[0]']
    assert set(native_border_styles(legacy).values()) != {'default'}


def test_disabled_native_colors_use_default_popup_and_menu_styles():
    from ls.core.terminal_ops.command_theme import menu_options, popup_options
    record = {'layout_mode': 'canvas-v3', 'native_layout': {'grouped_sections': True},
              'runtime_settings': {'colors_enabled': False}}
    assert popup_options(record) == ['-s', 'default', '-S', 'default']
    assert menu_options(record) == ['-s', 'default', '-S', 'default', '-H', 'default']


def test_custom_versioned_semantic_text_must_contrast_on_actual_title_surfaces():
    candidate = theme_definition('midnight')
    candidate['roles'].update(background='#000000', toolbar='#767676', success='#777777')
    assert _contrast(candidate['roles']['success'], candidate['roles']['background']) >= 4.5
    assert _contrast(candidate['roles']['success'], candidate['roles']['toolbar']) < 4.5
    with pytest.raises(ValueError, match='contrast'):
        validate_theme(candidate)
    # Pre-existing flat custom values retain the compatibility path.
    flat = candidate['roles']
    validate_theme(flat)
    assert palette({'runtime_settings': {'theme': flat}})['success'] == '#777777'

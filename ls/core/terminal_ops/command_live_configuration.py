"""Apply signed terminal tuning through the existing owned native canvas."""
from copy import deepcopy
import hashlib
from pathlib import Path
import time

from ..live_configuration import ConfigurationError
from ..live_configuration_cli import bound_runtime
from ..live_configuration_files import editable_json
from ..openpgp.container_storage import read_private_file
from ..openpgp.envelope import EnvelopeError
from .command_state import ShellCommandError, atomic_json, read_json, session_lock


def _fixed(settings):
    """Remove only qualified live tuning; all other settings remain frozen."""
    fixed = deepcopy(settings)
    for section in ('theme', 'context', 'progress'):
        fixed.pop(section, None)
    layout = fixed['layout']
    for name in ('quiet_seconds', 'expanded_min_rows', 'max_sections',
                 'activity_coalesce_ms', 'resize_debounce_ms'):
        layout.pop(name, None)
    for name in ('quiet_seconds', 'automatic_view_min_seconds', 'activity_coalesce_ms',
                 'resize_debounce_ms', 'ratio_step_percent'):
        layout['native_canvas'].pop(name, None)
    return fixed


def _runtime(service, binding):
    path = Path(binding['authority_file'])
    runtime, _defaults, digest, _write = bound_runtime(
        path, source_root=service.source_root, home=Path(binding['home']))
    raw = read_private_file(path, maximum=65536)
    if (digest != binding['binding_digest'] or hashlib.sha256(raw).hexdigest() != digest
            or editable_json(raw)['owners'].get(binding['owner']) != {'kind': 'terminal-runtime'}):
        raise ConfigurationError('configuration_binding_changed')
    return runtime


def _theme_matches(service, session, settings):
    from .command_theme import palette
    colors = palette(session | {'runtime_settings': settings})
    for name, foreground in (('pane-border-style', colors['text']),
                             ('pane-active-border-style', colors['selected'])):
        actual = service.backend._checked(session['endpoint'],
            ['show-options', '-w', '-t', session['status_window_id'], '-qv', name]).strip()
        if actual != f"fg={foreground},bg={colors['toolbar']}":
            raise ConfigurationError('configuration_native_pending')


def _apply(service, directory, binding, runtime, *, reconcile=False, owner=None, install=False):
    from .command_service import _digest
    with session_lock(directory):
        session = read_json(directory/'session.json', limit=2_000_000)
        if owner is not None:
            service._authorize(session, owner)
        service._require_family_root_live(session)
        existing = session.get('configuration_binding')
        if (session.get('state') != 'ready' or session.get('layout_mode') != 'canvas-v3'
                or existing != binding and not (install and existing is None)):
            raise ConfigurationError('configuration_session_changed')
        # Pending native writes are reconciled by explicit readback only.
        pending = session.get('configuration_pending')
        if pending is not None and not reconcile:
            raise ConfigurationError('configuration_native_pending')
        observed = service._verify(session)
        if observed.get('state') != 'ready':
            raise ConfigurationError('configuration_native_unknown')
        service._native_inventory(session, observed, service._strict_ops(directory, session), allow_active=True)
        if (session['native_layout'].get('overlay_lock') is not None
                or observed.get('controls', {}).get('freeze_layout')):
            raise ConfigurationError('configuration_native_busy')
        previous = session.get('configuration_applied')
        snapshot = runtime.store.load(
            minimum_revision=0 if previous is None else previous['revision'],
            ancestor_digest=None if previous is None else previous['digest'])
        if pending is not None:
            # A later signed revision may exist already. Reconcile the exact
            # retained candidate only if it belongs to the current lineage;
            # the next ordinary cadence can then apply the later revision.
            runtime.store.load(minimum_revision=pending['revision'], ancestor_digest=pending['digest'])
            snapshot = runtime.store._candidate(pending['digest'])
            if previous is not None and snapshot.revision < previous['revision']:
                raise ConfigurationError('configuration_conflict')
        runtime._validate_set(snapshot)
        if snapshot is None:
            raise ConfigurationError('configuration_unavailable')
        settings = snapshot.record['values'][binding['owner']]
        if _fixed(settings) != _fixed(session['runtime_settings']):
            raise ConfigurationError('configuration_native_restart_required')
        receipt = dict(revision=snapshot.revision, digest=snapshot.digest,
                       settings_digest=_digest(settings), provenance=snapshot.record['provenance'])
        if pending is not None and pending != receipt:
            raise ConfigurationError('configuration_conflict')
        if previous == receipt and pending is None:
            return {'ok': True, 'state': 'unchanged', **receipt}
        runtime.store.authority.check()
        if pending is not None:
            _theme_matches(service, session, settings)
        else:
            session['configuration_binding'] = binding
            session['configuration_pending'] = receipt
            atomic_json(directory/'session.json', session)
            service.backend.apply_native_control_theme(session | {'runtime_settings': settings})
        # Lost acknowledgements retain pending intent and the last accepted
        # settings. Explicit reconciliation never sends native writes again.
        runtime.store.authority.check()
        latest = runtime.store.load(minimum_revision=snapshot.revision, ancestor_digest=snapshot.digest)
        if pending is None and latest.digest != snapshot.digest:
            raise ConfigurationError('configuration_conflict')
        session.update(runtime_settings=settings, runtime_digest=receipt['settings_digest'],
                       configuration_applied=receipt,
                       ui_redraw_generation=session.get('ui_redraw_generation', 0)+1)
        session.pop('configuration_pending', None)
        session.pop('configuration_error', None)
        state = session['native_layout']
        state.update(desired_generation=state['desired_generation']+1, pending=True,
                     max_sections=settings['layout']['max_sections'])
        atomic_json(directory/'session.json', session)
        return {'ok': True, 'state': 'applied', **receipt}


def bind_configuration(service, handle, *, authority_file, config_owner, home, owner, reconcile=False):
    """Explicit controller binding; persisted preferences never grant ownership."""
    directory, session, operation_id = service._load(handle)
    if operation_id is not None:
        raise ConfigurationError('configuration_session_required')
    service._authorize(session, owner)
    if reconcile:
        binding = session.get('configuration_binding')
        if binding is None:
            raise ConfigurationError('configuration_unavailable')
    else:
        path = Path(authority_file)
        raw = read_private_file(path, maximum=65536)
        binding = dict(authority_file=str(path), binding_digest=hashlib.sha256(raw).hexdigest(),
                       owner=config_owner, home=str(home))
    runtime = _runtime(service, binding)
    return _apply(service, directory, binding, runtime, reconcile=reconcile, owner=owner, install=True)


class ConfigurationConsumer:
    """Reuse the shared runtime cadence inside the existing renderer loop."""
    def __init__(self):
        self.binding = self.runtime = None
        self.retry_at = 0.0

    def poll(self, service, directory, session):
        binding = session.get('configuration_binding')
        if binding is None or time.monotonic() < self.retry_at:
            return
        try:
            if binding != self.binding or self.runtime is None:
                self.runtime = _runtime(service, binding)
                self.binding = binding
                due = True
            else:
                due = self.runtime.poll(authorized=False)
            if due:
                _apply(service, directory, binding, self.runtime)
        except (ConfigurationError, ShellCommandError, EnvelopeError, OSError, ValueError, TypeError, KeyError) as error:
            # Failure retains the last applied revision. A pending native write
            # is never replayed by a later polling tick or renderer restart.
            self.retry_at = time.monotonic() + (60 if self.runtime is None else self.runtime.cadence)
            try:
                with session_lock(directory):
                    current = read_json(directory/'session.json', limit=2_000_000)
                    if current.get('configuration_binding') == binding:
                        current['configuration_error'] = getattr(error, 'code', 'configuration_unavailable')
                        atomic_json(directory/'session.json', current)
            except (ShellCommandError, OSError):
                pass

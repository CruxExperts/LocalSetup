"""Protected, explicitly read-only control recovery; never launches a model."""
import argparse
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

from ..branding import CLI_COMMAND
from .control_authority import launch_environment, selection
from .control_ready import challenge
from .run_cli import _grant, failure


def arguments(parser):
    for name in ('task', 'session', 'run', 'binding-sha256', 'control-nonce'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--grant', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--runtime-root', type=Path)
    channel = parser.add_mutually_exclusive_group(required=True)
    channel.add_argument('--encrypted-control-fd', type=int)
    channel.add_argument('--encrypted-control-socket', type=Path)
    parser.add_argument('--timeout', type=float, default=120)


def defaults(args):
    from .diagnostics import locations
    found = locations(Path.home())
    args.state_root = (args.state_root or Path(found['state'])).absolute()
    args.runtime_root = (args.runtime_root or Path(found['runtimes'])).absolute()
    args.workspace, args.grant = args.workspace.absolute(), args.grant.absolute()
    return args


def admission(args, *, protected=False):
    challenge(args.control_nonce)
    value, _ = _grant(args.grant, args.workspace)
    if (set(value) != {'schema_version', 'read', 'write', 'disclose', 'recipes', 'remote_control'}
            or any(value[name] for name in ('read', 'write', 'disclose', 'recipes'))):
        raise PermissionError('Recovery requires an explicit grant without workspace or process authority')
    selected = selection(value['remote_control'], args.workspace, protected=protected)
    if selected is None or selected.methods != frozenset({'reconcile'}):
        raise PermissionError('Recovery requires only reconciliation authority')
    return value, selected


def launch(argv, args):
    from .runtime_install import selected
    args = defaults(args)
    value, _ = admission(args)
    environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    environment.update(launch_environment(value['remote_control'], args.workspace))
    with selected(args.runtime_root, timeout=5, create=False) as release:
        executable = release/'venv/bin/python'
    inherited = None
    if args.encrypted_control_fd is not None:
        from .run_control import validate
        validate(args.encrypted_control_fd)
        inherited = os.get_inheritable(args.encrypted_control_fd)
        os.set_inheritable(args.encrypted_control_fd, True)
    try:
        os.execve(executable, [str(executable), '-I', '-B', '-m',
                              'ls.core.agent.control_recovery_cli', *argv], environment)
    finally:
        if inherited is not None:
            os.set_inheritable(args.encrypted_control_fd, inherited)


def execute(args, cancelled, *, expected_release):
    from .runtime_install import selected
    from .session_owner import lease, _separate
    from .control_run import listen
    from .run_io import Streams
    _, current = admission(args, protected=True)
    _separate(args.state_root, args.workspace)
    _separate(args.state_root, args.runtime_root)
    if args.encrypted_control_socket is not None:
        _separate(args.encrypted_control_socket.parent, args.workspace)
    expires = time.monotonic()+args.timeout
    streams = Streams(expires, cancelled)
    # Keep the exact selected runtime and existing session leased throughout.
    with selected(args.runtime_root, timeout=5, create=False) as release:
        if release != expected_release:
            raise PermissionError('Selected recovery runtime changed across protected startup')
        with lease(args.state_root/'sessions', task=args.task, session=args.session,
                   workspace=args.workspace, expires=expires, revoked=cancelled, create=False) as owner:
            with listen(args.encrypted_control_fd, current, owner, args.run,
                        cancelled=cancelled, ready=lambda data: streams.event(1, 'control_ready', data),
                        nonce=args.control_nonce, socket_path=args.encrypted_control_socket,
                        recover_digest=args.binding_sha256):
                while not cancelled.is_set() and time.monotonic() < expires:
                    cancelled.wait(min(.05, max(0, expires-time.monotonic())))
    return 130 if cancelled.is_set() else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog=f'{CLI_COMMAND} control-recover')
    arguments(parser)
    args = defaults(parser.parse_args(argv))
    if not sys.flags.isolated or not sys.dont_write_bytecode or not Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        return failure('text', 0, 'failed', 3, 'control recovery requires its protected installed runtime.')
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        return failure('text', 0, 'failed', 2, 'control recovery timeout must be within 3600 seconds.')
    cancelled = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancelled.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    owned = False
    try:
        if args.encrypted_control_fd is not None:
            from .run_control import validate
            validate(args.encrypted_control_fd)
            os.set_inheritable(args.encrypted_control_fd, False)
            owned = True
        return execute(args, cancelled, expected_release=Path(sys.prefix).parent)
    except (OSError, ValueError, TypeError, RuntimeError):
        return failure('text', 0, 'failed', 2, 'control recovery denied or unavailable; retain the original operation evidence.')
    finally:
        if owned:
            os.close(args.encrypted_control_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())

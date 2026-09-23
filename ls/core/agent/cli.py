"""LSCli's provider-free command boundary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from ..branding import CLI_COMMAND, CLI_NAME, PRODUCT_NAME
from ..framework_version import framework_version
from .diagnostics import inspect


def main(argv: list[str] | None = None, *, default_runtime_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog=CLI_COMMAND, description=f"{CLI_NAME}: the integrated {PRODUCT_NAME} agent CLI")
    parser.add_argument("--version", action="version", version=f"{CLI_NAME} ({PRODUCT_NAME}) {framework_version()}")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="Inspect installed payload and execution readiness without provider access")
    doctor.add_argument("--format", choices=("text", "json"), default="text")
    doctor.add_argument("--runtime-root", type=Path)
    doctor.add_argument("--profiles", type=Path)
    setup = commands.add_parser("setup", help="Plan or apply explicit runtime, profile or command setup")
    mode = setup.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--reselect", metavar="SHA256")
    mode.add_argument("--registration-status", action="store_true")
    setup.add_argument("--bin-dir", type=Path)
    setup.add_argument("--registration-sha256")
    registration_mode = setup.add_mutually_exclusive_group()
    registration_mode.add_argument("--refresh-registration", action="store_true")
    registration_mode.add_argument("--recover-registration", action="store_true")
    setup.add_argument("--profile-input", type=Path)
    setup.add_argument("--profiles", type=Path)
    setup.add_argument("--profile-sha256")
    setup.add_argument("--wheel", type=Path)
    setup.add_argument("--sha256")
    setup.add_argument("--wheelhouse", type=Path)
    setup.add_argument("--sandbox-bundle", type=Path)
    setup.add_argument("--sandbox-sha256")
    setup.add_argument("--runtime-root", type=Path)
    setup.add_argument("--timeout", type=float, default=300)
    sessions=commands.add_parser('sessions',help='List local session metadata without provider access')
    sessions.add_argument('--state-root',type=Path)
    sessions.add_argument('--format',choices=('text','json'),default='text')
    compact = commands.add_parser('compact', help='Compact an explicitly disclosed checkpoint in the protected runtime')
    from .compact_cli import arguments as compact_arguments
    compact_arguments(compact)
    branch = commands.add_parser('branch', help='Copy settled compatible history into a fresh session')
    from .session_branch import arguments as branch_arguments
    branch_arguments(branch)
    profiles = commands.add_parser('profiles', help='List configured models without provider access')
    profiles.add_argument('--profiles', type=Path)
    profiles.add_argument('--format', choices=('text', 'json'), default='text')
    run = commands.add_parser('run', help='Run with an explicit profile and task grant in the protected runtime')
    from .run_options import arguments
    arguments(run)
    read = commands.add_parser('read', help='Stream bounded verified pages from a granted local file')
    read.add_argument('--grant', type=Path, required=True, help='Private read/disclosure grant JSON')
    read.add_argument('--workspace', type=Path, default=Path.cwd())
    read.add_argument('--path', required=True, help='Workspace-relative granted file')
    read.add_argument('--max-pages', type=int, default=1,
                      help='Maximum pages in this invocation (default: 1; maximum: 2048)')
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(effective_argv)
    uses_recorded_or_profile_root = args.command == 'setup' and (args.profile_input is not None or args.registration_status or args.refresh_registration or args.recover_registration)
    if default_runtime_root is not None and hasattr(args, 'runtime_root') and args.runtime_root is None and not uses_recorded_or_profile_root:
        effective_argv.extend(['--runtime-root', str(default_runtime_root)])
        args = parser.parse_args(effective_argv)
    if args.command == 'read':
        from .run_cli import _grant, _openpgp_authority
        from .file_broker import FileBroker
        from .file_grants import FileGrant
        import tempfile
        import threading
        import time
        import uuid
        if not 1 <= args.max_pages <= 2048:
            read.error('--max-pages must be between 1 and 2048')
        try:
            workspace = args.workspace.absolute()
            value, _ = _grant(args.grant.absolute(), workspace)
            openpgp = _openpgp_authority(value.get('openpgp')) or {}
            task, session = uuid.uuid4().hex, uuid.uuid4().hex
            expires = time.monotonic() + 300
            grant = FileGrant(task, session, workspace, tuple(value['read']), (),
                              tuple(value['disclose']), expires, revoked=threading.Event())
            with tempfile.TemporaryDirectory(prefix='lscli-read-') as temporary:
                broker = FileBroker(grant, Path(temporary))
                try:
                    cursor = None
                    for _ in range(args.max_pages):
                        page = broker.read_page(task, session, args.path, cursor,
                                                for_provider=True, **openpgp)
                        print(json.dumps(page, sort_keys=True, separators=(',', ':'), ensure_ascii=True), flush=True)
                        cursor = page['next_cursor']
                        if cursor is None:
                            return 0
                    print('More content remains; rerun with a larger --max-pages. '
                          'CLI cursors expire when this process exits.', file=sys.stderr)
                    return 0
                finally:
                    broker.clear_page_cache()
        except (OSError, ValueError, TypeError, PermissionError, TimeoutError):
            print(f'{CLI_NAME} read denied or unavailable; inspect the grant, file, and authority.', file=sys.stderr)
            return 2
    if args.command == 'compact':
        from .compact_cli import launch as launch_compact
        from .run_cli import failure
        try:
            launch_compact(effective_argv[1:], args)
        except (OSError, ValueError, TypeError, RuntimeError):
            return failure('text', 0, 'failed', 3, 'compaction could not start; verify profile, credential and runtime.')
    if args.command == 'branch':
        from .session_branch import main as branch_main
        return branch_main(args)
    if args.command == 'profiles':
        from .profile_inventory import main as profile_inventory
        return profile_inventory(args.profiles, args.format)
    if args.command == 'sessions':
        from .diagnostics import locations
        from .session_index import scan
        from .run_io import Streams, safe
        import threading
        import time
        try:
            root=args.state_root or Path(locations(Path.home())['state'])
            expires=time.monotonic()+5
            report=scan(root/'sessions',expires=expires)
            text=json.dumps(report,ensure_ascii=True)+'\n' if args.format=='json' else ''.join(
                safe(f"{item.get('session',item['storage_id'])}: {item['status']}")+'\n' for item in report['sessions'])
            Streams(expires,threading.Event()).write(text)
            return 0
        except KeyboardInterrupt:return 130
        except (OSError,ValueError,TypeError,RecursionError):
            from .run_cli import failure
            return failure('text',0,'failed',2,'session inventory failed; verify private state integrity.')
    if args.command == 'run':
        from .run_cli import launch
        try:
            launch(effective_argv[1:],args)
        except (OSError,ValueError,TypeError,RuntimeError):
            print(f"{CLI_NAME} run could not start; verify the selected profile, credential and installed runtime.",file=sys.stderr)
            return 3
    if args.command == "setup" and (args.bin_dir is not None or args.registration_status or args.registration_sha256 is not None or args.refresh_registration or args.recover_registration):
        if args.bin_dir is None:
            setup.error("Registration options require --bin-dir")
        if any(value is not None for value in (args.profile_input, args.profiles, args.profile_sha256, args.wheel,
                args.sha256, args.wheelhouse, args.sandbox_bundle, args.sandbox_sha256,
                args.reselect)) or args.timeout != 300:
            setup.error("Registration cannot be combined with profile or runtime installation inputs")
        if (args.registration_status or args.refresh_registration or args.recover_registration) and args.runtime_root is not None:
            setup.error("Owned registration operations use the recorded runtime; omit --runtime-root")
        if args.registration_status and (args.refresh_registration or args.recover_registration):
            setup.error("Registration status cannot be combined with refresh or recovery")
        if args.apply and not args.registration_sha256:
            setup.error("Registration apply requires --registration-sha256 from its reviewed plan")
        if not args.apply and args.registration_sha256 is not None:
            setup.error("--registration-sha256 applies only to registration application")
        from .registration_cli import main as register_command
        return register_command(args)
    if args.command == "setup" and args.profile_input is not None:
        if any((args.wheel, args.sha256, args.wheelhouse, args.sandbox_bundle,
                args.sandbox_sha256, args.runtime_root, args.reselect)) or args.timeout != 300:
            setup.error("Profile setup cannot be combined with runtime installation options")
        if args.apply and not args.profile_sha256:
            setup.error("Profile apply requires --profile-sha256 from its reviewed plan")
        if args.plan and args.profile_sha256:
            setup.error("--profile-sha256 applies only to profile creation")
        from .profile_setup_cli import main as configure_profiles
        return configure_profiles(args)
    if args.command == "setup":
        if args.profiles is not None or args.profile_sha256 is not None:
            setup.error("Profile options require --profile-input")
        from .diagnostics import locations
        from .runtime_install import install, plan, reselect
        root = args.runtime_root or Path(locations(Path.home())["runtimes"])
        if args.reselect and any((args.wheel, args.sha256, args.wheelhouse, args.sandbox_bundle, args.sandbox_sha256)):
            setup.error("--reselect cannot be combined with installation inputs")
        if not args.reselect and not all((args.wheel, args.sha256, args.wheelhouse)):
            setup.error("--plan and --apply require --wheel, --sha256 and --wheelhouse")
        if (args.sandbox_bundle is None) != (args.sandbox_sha256 is None):
            setup.error("--sandbox-bundle and --sandbox-sha256 must be supplied together")
        native_inputs = {} if args.sandbox_bundle is None else {
            'sandbox_bundle': args.sandbox_bundle, 'sandbox_sha256': args.sandbox_sha256}
        inputs = (root, args.wheel, args.sha256, args.wheelhouse, Path.cwd())
        try:
            if args.reselect:
                result = reselect(root, args.reselect, timeout=args.timeout)
            else:
                result = install(*inputs, timeout=args.timeout, **native_inputs) if args.apply else plan(*inputs, **native_inputs)
        except KeyboardInterrupt:
            print(f"{CLI_NAME} setup cancelled; inspect retained installation state.", file=sys.stderr)
            return 130
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError, subprocess.SubprocessError) as exc:
            print(f"{CLI_NAME} setup failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command is None:
        print(f"{CLI_NAME}: use '{CLI_COMMAND} run --help' for explicit coding grants or '{CLI_COMMAND} doctor' for readiness.", file=sys.stderr)
        return 3
    from .doctor_output import emit
    try:
        options = {}
        if args.runtime_root is not None:
            options['runtime_root'] = args.runtime_root
        if args.profiles is not None:
            options['profiles_path'] = args.profiles
        report = inspect(**options)
        emit(report, args.format)
        return 0 if report['status'] == 'static_verified' else 3
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        return 2



if __name__ == "__main__":
    raise SystemExit(main())

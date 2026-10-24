"""LSCli's provider-free command boundary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from ..branding import CLI_COMMAND, CLI_NAME, PRODUCT_NAME
from ..framework_version import framework_version
from .diagnostics import inspect


def main(argv: list[str] | None = None, *, default_runtime_root: Path | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] == 'complete':
        # Preserve the completion owner's JSON-only, redacted error boundary.
        from .completion_cli import main as completion_main
        return completion_main(effective_argv, default_runtime_root=default_runtime_root)
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
    mode.add_argument("--download-release", metavar="TAG", help="Download one explicitly trusted release without installing it")
    mode.add_argument("--prepare-dependencies", action="store_true", help="Prepare hash-locked dependency artifacts without selecting a runtime")
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
    setup.add_argument("--release-manifest", type=Path)
    setup.add_argument("--release-sha256", help="Independently trusted runtime manifest SHA-256")
    setup.add_argument("--assets-dir", type=Path, help="Fresh directory for explicit release or dependency acquisition")
    setup.add_argument("--python", help="Python with pip for --prepare-dependencies (default: this interpreter)")
    setup.add_argument("--sdk-only", action="store_true", help="Install a manifest's SDK runtime without its native sandbox")
    setup.add_argument("--runtime-root", type=Path)
    setup.add_argument("--timeout", type=float, default=300)
    sessions=commands.add_parser('sessions',help='List local session metadata without provider access')
    sessions.add_argument('--state-root',type=Path)
    sessions.add_argument('--format',choices=('text','json'),default='text')
    events = commands.add_parser('events', help='Read bounded retained event pages without provider access')
    from .event_cli import arguments as event_arguments
    event_arguments(events)
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
    relay = commands.add_parser('control-relay', help='Forward encrypted control frames to an explicit private run socket')
    relay.add_argument('--socket', type=Path, required=True)
    relay.add_argument('--timeout', type=float, default=300)
    http_relay = commands.add_parser('control-http-relay', help='Serve encrypted HTTP control through an explicit private run socket')
    http_relay.add_argument('--socket', type=Path, required=True)
    http_relay.add_argument('--bind', default='127.0.0.1', help='Literal local bind IP (default: loopback)')
    http_relay.add_argument('--port', type=int, required=True)
    http_relay.add_argument('--timeout', type=float, default=300)
    complete = commands.add_parser('complete', help='Make one tool-free request with optional granted context')
    from .completion_cli import configure as completion_arguments
    completion_arguments(complete)
    read = commands.add_parser('read', help='Stream bounded verified pages from a granted local file')
    read.add_argument('--grant', type=Path, required=True, help='Private read/disclosure grant JSON')
    read.add_argument('--workspace', type=Path, default=Path.cwd())
    read.add_argument('--path', required=True, help='Workspace-relative granted file')
    read.add_argument('--max-pages', type=int, default=1,
                      help='Maximum pages in this invocation (default: 1; maximum: 2048)')
    args = parser.parse_args(effective_argv)
    if args.command == 'control-http-relay':
        from .control_http_server import main as http_relay_main
        return http_relay_main(args)
    if args.command == 'control-relay':
        from .control_relay import main as relay_main
        return relay_main(args)
    if args.command == 'events':
        from .event_cli import main as event_main
        return event_main(args)
    uses_recorded_or_profile_root = args.command == 'setup' and (args.profile_input is not None or args.registration_status or args.refresh_registration or args.recover_registration or args.download_release or args.prepare_dependencies)
    if default_runtime_root is not None and hasattr(args, 'runtime_root') and args.runtime_root is None and not uses_recorded_or_profile_root:
        effective_argv.extend(['--runtime-root', str(default_runtime_root)])
        args = parser.parse_args(effective_argv)
    if args.command == 'read':
        from .run_cli import _grant, _openpgp_authority
        from .file_broker import FileBroker
        from .file_grants import FileGrant
        from .container_authority import reader as container_reader
        from ..openpgp.container_reader import ContainerReadError
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
                broker = FileBroker(grant, Path(temporary), container_reader=container_reader(
                    value.get('container_runtime'), workspace, deadline=expires,
                    check=lambda: grant.check(task, session, 'read', args.path, provider=True)))
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
        except ContainerReadError as error:
            print(json.dumps({'state': error.code, 'kind': 'protected_read'}, sort_keys=True), file=sys.stderr)
            return 2
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
    if args.command == 'setup' and args.prepare_dependencies:
        if not args.assets_dir:
            setup.error('--prepare-dependencies requires a fresh --assets-dir')
        if (any(value is not None for value in (args.wheelhouse, args.sandbox_bundle,
                args.sandbox_sha256, args.runtime_root, args.profile_input, args.profiles,
                args.profile_sha256, args.bin_dir, args.registration_sha256))
                or args.sdk_only or args.refresh_registration or args.recover_registration):
            setup.error('Dependency preparation cannot be combined with installation, profile or registration inputs')
        if args.release_manifest:
            if not args.release_sha256 or args.wheel or args.sha256:
                setup.error('Dependency preparation requires a trusted manifest or a trusted wheel, not both')
        elif not args.wheel or not args.sha256 or args.release_sha256:
            setup.error('Dependency preparation requires --wheel and --sha256, or a trusted release manifest')
        from .dependency_artifacts import prepare
        try:
            if args.release_manifest:
                from .release_manifest import resolve
                resolved = resolve(args.release_manifest, args.release_sha256)
                wheel, digest = resolved['wheel'], resolved['sha256']
            else:
                wheel, digest = args.wheel, args.sha256
            result = prepare(wheel, digest, args.assets_dir, python=args.python, timeout=args.timeout)
        except KeyboardInterrupt:
            print(f'{CLI_NAME} dependency preparation cancelled; inspect retained state.', file=sys.stderr)
            return 130
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
            print(f'{CLI_NAME} dependency preparation failed: {exc}', file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == 'setup' and args.python is not None:
        setup.error('--python requires --prepare-dependencies')
    if args.command == 'setup' and args.download_release:
        if not args.assets_dir or not args.release_sha256:
            setup.error('--download-release requires --assets-dir and --release-sha256')
        if (any(value is not None for value in (args.wheel, args.sha256, args.wheelhouse,
                args.sandbox_bundle, args.sandbox_sha256, args.release_manifest, args.runtime_root,
                args.profile_input, args.profiles, args.profile_sha256, args.bin_dir,
                args.registration_sha256)) or args.sdk_only or args.refresh_registration or args.recover_registration):
            setup.error('Release download cannot be combined with installation, profile or registration inputs')
        from .release_download import download
        try:
            result = download(args.download_release, args.release_sha256, args.assets_dir, timeout=args.timeout)
        except KeyboardInterrupt:
            print(f'{CLI_NAME} download cancelled; inspect retained files.', file=sys.stderr)
            return 130
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError, zipfile.BadZipFile) as exc:
            print(f'{CLI_NAME} download failed: {exc}', file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == 'setup' and args.assets_dir is not None:
        setup.error('--assets-dir requires --download-release or --prepare-dependencies')
    if args.command == "setup" and (args.bin_dir is not None or args.registration_status or args.registration_sha256 is not None or args.refresh_registration or args.recover_registration):
        if args.bin_dir is None:
            setup.error("Registration options require --bin-dir")
        if any(value is not None for value in (args.profile_input, args.profiles, args.profile_sha256, args.wheel,
                args.sha256, args.wheelhouse, args.sandbox_bundle, args.sandbox_sha256,
                args.reselect, args.release_manifest, args.release_sha256)) or args.sdk_only or args.timeout != 300:
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
                args.sandbox_sha256, args.runtime_root, args.reselect, args.release_manifest,
                args.release_sha256, args.sdk_only)) or args.timeout != 300:
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
        if args.reselect and any((args.wheel, args.sha256, args.wheelhouse, args.sandbox_bundle,
                                 args.sandbox_sha256, args.release_manifest, args.release_sha256, args.sdk_only)):
            setup.error("--reselect cannot be combined with installation inputs")
        if args.release_manifest:
            if not args.release_sha256 or not args.wheelhouse:
                setup.error('--release-manifest requires --release-sha256 and --wheelhouse')
            if any((args.wheel, args.sha256, args.sandbox_bundle, args.sandbox_sha256)):
                setup.error('Manifest setup cannot be combined with individual wheel or sandbox inputs')
        elif args.release_sha256 or args.sdk_only:
            setup.error('--release-sha256 and --sdk-only require --release-manifest')
        elif not args.reselect and not all((args.wheel, args.sha256, args.wheelhouse)):
            setup.error("--plan and --apply require --wheel, --sha256 and --wheelhouse, or a trusted release manifest")
        if (args.sandbox_bundle is None) != (args.sandbox_sha256 is None):
            setup.error("--sandbox-bundle and --sandbox-sha256 must be supplied together")
        native_inputs = {} if args.sandbox_bundle is None else {
            'sandbox_bundle': args.sandbox_bundle, 'sandbox_sha256': args.sandbox_sha256}
        inputs = (root, args.wheel, args.sha256, args.wheelhouse, Path.cwd())
        try:
            release_identity = {}
            if args.release_manifest:
                from .release_manifest import resolve
                resolved = resolve(args.release_manifest, args.release_sha256, sdk_only=args.sdk_only)
                inputs = (root, resolved['wheel'], resolved['sha256'], args.wheelhouse, Path.cwd())
                native_inputs = resolved['native_inputs']
                release_identity = resolved['release']
            if args.reselect:
                result = reselect(root, args.reselect, timeout=args.timeout)
            else:
                result = install(*inputs, timeout=args.timeout, **native_inputs) if args.apply else plan(*inputs, **native_inputs)
            result.update(release_identity)
        except KeyboardInterrupt:
            print(f"{CLI_NAME} setup cancelled; inspect retained installation state.", file=sys.stderr)
            return 130
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
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

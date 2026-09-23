"""Explicit protected public coding dispatch; saved configuration is not task authority."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from ..branding import CLI_COMMAND, CLI_NAME
from .broker_rpc import _decode
from .profiles import load, wire
from .run_options import arguments, defaults
from .runtime_lock import _directory

_CREDENTIAL = "LOCALSETUP_RUN_CREDENTIAL"
_PROFILE = "LOCALSETUP_RUN_PROFILE"
_OPENPGP_SECRET = "LOCALSETUP_RUN_OPENPGP_PASSPHRASE"


def launch(argv, args):
    """Replace the bootstrap with sealed Python, forwarding only selected credentials."""
    from .runtime_install import selected
    from .coding_protocol import profile_digest
    args = defaults(args)
    profile = load(args.profiles,args.profile)
    credential = profile.credential({profile.credential_env:os.environ.get(profile.credential_env,'')})
    with selected(args.runtime_root,timeout=5) as release:
        executable = release/'venv/bin/python'
    grant, _ = _grant(args.grant, args.workspace)
    openpgp = _openpgp_authority(grant.get('openpgp'))
    environment = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',_CREDENTIAL:credential,
                   _PROFILE:profile_digest(wire(profile))}
    if openpgp is not None:
        from ..openpgp.secrets import SecretReference, SecretProvider
        reference = openpgp['passphrase_reference']
        if isinstance(reference, SecretReference) and reference.provider is SecretProvider.ENV:
            name = reference.name
            if name in environment or name == profile.credential_env:
                raise ValueError('OpenPGP passphrase reference collides with protected launch values')
            if name in os.environ:
                environment[_OPENPGP_SECRET] = os.environ[name]
    inherited = None
    if args.control_fd is not None:
        from .run_control import validate
        validate(args.control_fd)
        inherited = os.get_inheritable(args.control_fd)
        os.set_inheritable(args.control_fd, True)
    try:
        os.execve(executable,[str(executable),'-I','-B','-m','ls.core.agent.run_cli',*argv],environment)
    finally:
        if inherited is not None:
            os.set_inheritable(args.control_fd, inherited)


def _grant(path, workspace):
    from .session_owner import _separate
    from .process_rpc import Recipe
    _separate(path,workspace)
    fd = _directory(path.parent)
    os.close(fd)
    # Bound the actual opened inode, not an earlier path stat.
    import stat
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o7077:
            raise ValueError('Grant must be a private owned regular file')
        raw = os.read(fd,1024*1024+1)
    finally:
        os.close(fd)
    if len(raw)>1024*1024:
        raise ValueError('Grant exceeds 1 MiB')
    value = _decode(raw)
    if (not isinstance(value,dict)
            or set(value) not in (
                {'schema_version','read','write','disclose','recipes'},
                {'schema_version','read','write','disclose','recipes','openpgp'})
            or type(value['schema_version']) is not int
            or value['schema_version']!=1):
        raise ValueError('Unsupported run grant schema')
    for key in ('read','write','disclose'):
        if not isinstance(value[key],list) or len(value[key])>256 or any(not isinstance(x,str) for x in value[key]):
            raise ValueError('Invalid grant scopes')
    if not isinstance(value['recipes'],dict) or len(value['recipes'])>64:
        raise ValueError('Invalid recipe inventory')
    authority = _openpgp_authority(value.get('openpgp'))
    if authority is not None:
        _separate(authority['gnupg_home'], workspace)
        executable = value['openpgp']['passphrase'].get('executable')
        if executable is not None:
            _separate(Path(executable), workspace)
    recipes = {}
    for name, recipe in value['recipes'].items():
        if not isinstance(recipe,dict) or set(recipe)!={'command','files','seconds'} or not isinstance(recipe['command'],list) or not isinstance(recipe['files'],list):
            raise ValueError('Invalid recipe schema')
        recipes[name] = Recipe(tuple(recipe['command']),tuple(recipe['files']),recipe['seconds'])
    return value, recipes

def _openpgp_authority(config, *, protected=False):
    """Build only trusted supervisor read authority from private grant JSON."""
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) != {
            'home', 'expected_signers', 'expected_recipients',
            'trusted_fingerprints', 'passphrase', 'owner_fingerprint',
            'publisher_fingerprint'}:
        raise ValueError('Invalid OpenPGP read authority schema')
    if not isinstance(config['home'], str) or not Path(config['home']).is_absolute():
        raise ValueError('OpenPGP home must be an absolute path')
    passphrase = config['passphrase']
    if (not isinstance(passphrase, dict)
            or set(passphrase) not in ({'provider', 'name'}, {'provider', 'name', 'executable'})):
        raise ValueError('Invalid OpenPGP passphrase reference')
    from ..openpgp.contracts import EnvelopePolicy, LocalTrust
    from ..openpgp.secrets import SecretProvider, SecretReference, SecretResolver
    reference = SecretReference(SecretProvider(passphrase['provider']), passphrase['name'])
    binary = passphrase.get('executable', 'envman')
    if ('executable' in passphrase and
            (reference.provider is not SecretProvider.ENVMAN
             or not isinstance(binary, str)
             or not Path(binary).is_absolute())):
        raise ValueError('Envman executable must be an explicitly selected absolute path')
    if any(not isinstance(config[key], list) or len(config[key]) > 32 for key in (
            'expected_signers', 'expected_recipients', 'trusted_fingerprints')):
        raise ValueError('Invalid OpenPGP authority fingerprints')
    if reference.name in {_CREDENTIAL, _PROFILE, _OPENPGP_SECRET}:
        raise ValueError('OpenPGP passphrase reference collides with run credentials')
    return {
        'gnupg_home': Path(config['home']),
        'policy': EnvelopePolicy(
            config['expected_signers'], config['expected_recipients'],
            config['owner_fingerprint'], config['publisher_fingerprint'],
        ),
        'local_trust': LocalTrust(frozenset(config['trusted_fingerprints'])),
        'passphrase_reference': reference,
        'secret_resolver': SecretResolver(
            envman_binary=binary,
            env_values={reference.name: os.environ.get(_OPENPGP_SECRET, '')}
            if protected and reference.provider is SecretProvider.ENV else None,
        ),
    }


def _state(root):
    # Create only this explicit private tree, checking every existing ancestor.
    missing = []
    parent = root
    while not parent.exists():
        missing.append(parent); parent = parent.parent
    fd = _directory(parent);os.close(fd)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    fd = _directory(root)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise ValueError('Run state must be private')
        for name in ('sessions','leases','snapshots','scratch'):
            try:os.mkdir(name,0o700,dir_fd=fd)
            except FileExistsError:pass
            child = _directory(root/name)
            try:
                if os.fstat(child).st_mode & 0o077:raise ValueError('Run state child must be private')
            finally:os.close(child)
        os.fsync(fd)
    finally:os.close(fd)


def execute(args, streams, cancelled, steering=None, approvals=None):
    from .coding_protocol import request, profile_digest
    from .coding_run import CodingGrant,RunPaths,run_coding,disclosure_digest
    from .file_grants import FileGrant
    from .operation_journal import IDENTIFIER
    from .resource_group import Limits
    from .session_owner import _separate
    from .run_io import safe
    profile = load(args.profiles,args.profile)
    if profile_digest(wire(profile)) != os.environ.get(_PROFILE):
        raise ValueError('Provider profile changed across protected startup')
    _separate(args.profiles,args.workspace)
    grant, recipes = _grant(args.grant,args.workspace)
    openpgp = _openpgp_authority(grant.get('openpgp'), protected=True)
    from .context_files import selection, include
    selected_context=selection(args.context,args.skill)
    from .image_inputs import paths as image_paths, load as load_images
    selected_images=image_paths(args.image)
    new_session = getattr(args, 'require_new_session', False)
    if new_session and (selected_context or selected_images):
        raise ValueError('Atomic fresh session mode excludes explicit context, skills and images')
    if selected_images and 'images' not in profile.capabilities:
        raise ValueError('Selected profile must explicitly support images')
    if (args.resume or args.recover_from) and (not args.task or not args.session):
        raise ValueError('History requires explicit recorded task and session')
    task, session = args.task or uuid.uuid4().hex, args.session or uuid.uuid4().hex
    if not IDENTIFIER.fullmatch(task) or not IDENTIFIER.fullmatch(session):
        raise ValueError('Invalid task/session identity')
    paths = RunPaths(args.runtime_root,args.state_root/'sessions',args.state_root/'leases',
                     args.state_root/'snapshots',args.state_root/'scratch',args.resource_parent)
    paths.validate(args.workspace)
    _separate(args.state_root,args.workspace)
    _separate(args.state_root,args.runtime_root)
    if steering is not None:
        steering.bind(task,session,args.profile)
    if approvals is not None:
        approvals.bind(task,session,args.profile)
    prompt = streams.prompt()
    raw_profile = wire(profile)
    payload = {'schema_version':1,'run_id':uuid.uuid4().hex,'profile':raw_profile,
        'credential':profile.credential({profile.credential_env:os.environ.get(_CREDENTIAL,'')}),
        'prompt':prompt,'instructions':'Follow the user task within the explicitly granted tools.','history':None,
        'request_limit':args.request_limit,'tool_limit':args.tool_limit,'token_limit':args.token_limit}
    request(payload)
    files = FileGrant(task,session,args.workspace,tuple(grant['read']),tuple(grant['write']),tuple(grant['disclose']),streams.expires,revoked=cancelled)
    resume=args.resume
    if resume or args.recover_from:
        from .session_owner import lease
        with lease(paths.sessions,task=task,session=session,workspace=args.workspace,
                   expires=streams.expires,revoked=cancelled,create=False) as owner:
            if args.recover_from:
                from .recovery import recover_checkpoint
                resume=recover_checkpoint(owner,args.runtime_root,args.recover_from,
                                          profile=profile_digest(raw_profile),recipes=recipes)
            payload['history']=owner.resume_checkpoint(resume,profile=profile_digest(raw_profile)).decode()
    else:
        _state(args.state_root)
    if selected_context or selected_images:
        from .file_broker import FileBroker
        from .session_owner import lease
        with lease(paths.sessions,task=task,session=session,workspace=args.workspace,
                   expires=streams.expires,revoked=cancelled,create=not bool(resume)) as owner:
            broker=FileBroker(files,paths.target_leases)
            payload['prompt']=include(prompt,selected_context,owner,broker)
            if selected_images:
                payload['images']=load_images(owner,broker,selected_images)
    authority = CodingGrant(task,session,disclosure_digest(payload),streams.expires,revoked=cancelled)
    sequence = 0
    def emit(kind,data):
        nonlocal sequence
        sequence += 1
        if args.format=='jsonl':streams.event(sequence,kind,data)
    emit('start',{'task':task,'session':session,'profile':args.profile})
    def progress(event):
        if args.format=='jsonl':emit('progress',event)
        elif event.get('event_kind')=='part_start' and event.get('part',{}).get('part_kind')=='tool-call':
            streams.write('Tool: '+safe(event['part'].get('tool_name',''))+'\n')
    def approve(method,data,recipes,check):
        sink=streams.approval if args.interactive else lambda value:emit('approval_request',value)
        return approvals.require(method,data,recipes,sink,check)
    outcome = run_coding(paths,payload,authority,files,recipes,limits=Limits(),on_event=progress,
                         cancel=cancelled,expected_release=Path(sys.prefix).parent,resume=resume,new_session=new_session,
                         steering=None if steering is None else steering.take,
                         approve=None if approvals is None else approve, openpgp=openpgp)
    codes = {'completed':0,'cancelled':130,'timed_out':124,'output_limit':5,'failed':1}
    result = {'status':outcome.status,'task':task,'session':session}
    if outcome.data is not None:
        result.update(output=outcome.data['output'],checkpoint=outcome.data['checkpoint'])
    finish_input(streams)
    # Use a small bounded terminal-delivery window after cancellation/deadline.
    from .run_io import Streams
    terminal = Streams(time.monotonic()+1,threading.Event())
    if args.format=='jsonl':terminal.event(sequence+1,'result',result)
    elif outcome.data is not None:terminal.write(safe(outcome.data['output'])+'\n')
    else:terminal.write('Run '+outcome.status+'\n')
    return codes.get(outcome.status,1)


def finish_input(streams):
    if hasattr(streams,'close'):
        streams.close()


def failure(format, sequence, status, code, diagnostic, *, output_fd=1, diagnostic_fd=2):
    from .run_io import Streams
    deadline=time.monotonic()+1
    try:Streams(time.monotonic()+0.1,threading.Event(),output_fd=diagnostic_fd).write(f'{CLI_NAME} {diagnostic}\n')
    except (OSError,TimeoutError):pass
    if format=='jsonl':
        try:Streams(deadline,threading.Event(),output_fd=output_fd).event(sequence+1,'result',{'status':status})
        except (OSError,TimeoutError):pass
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog=CLI_COMMAND+' run')
    arguments(parser);args=defaults(parser.parse_args(argv))
    if not sys.flags.isolated or not sys.dont_write_bytecode or not Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        print(f'{CLI_NAME} run requires its protected installed runtime.',file=sys.stderr);return 3
    if not math.isfinite(args.timeout) or not 0<args.timeout<=3600:
        parser.error('--timeout must be within 0..3600 seconds')
    if args.interactive and (args.control_fd is not None or args.format != 'text'):
        parser.error('--interactive requires text output and excludes --control-fd')
    if args.approve_tools and not args.interactive and (args.control_fd is None or args.format != 'jsonl'):
        parser.error('--approve-tools requires --control-fd and --format jsonl')
    cancelled=threading.Event()
    previous={sig:signal.signal(sig,lambda *_:cancelled.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
    from .run_io import Streams
    streams=Streams(time.monotonic()+args.timeout,cancelled)
    def failed(status, code, diagnostic):
        finish_input(streams)
        return failure(args.format,streams.sequence,status,code,diagnostic)
    try:
        from .run_control import listen
        from .steering import Steering
        steering=Steering(cancelled,streams.expires)
        from .approvals import Approvals
        approvals=Approvals() if args.approve_tools or args.interactive else None
        if args.interactive:
            from .interactive import Terminal
            streams=Terminal(streams.expires,cancelled)
            streams.steering,streams.approvals=steering,approvals
        with listen(args.control_fd,cancelled,streams.expires,steering,approvals):
            return execute(args,streams,cancelled,steering,approvals)
    except (InterruptedError,KeyboardInterrupt):
        return failed('cancelled',130,'run cancelled.')
    except TimeoutError:
        return failed('timed_out',124,'run deadline expired; inspect session evidence.')
    except BrokenPipeError:
        return 1
    except (OSError,ValueError,TypeError,RuntimeError,RecursionError):
        return failed('failed',2,'run failed; verify explicit profiles, grants, runtime readiness and session evidence.')
    finally:
        if args.interactive and hasattr(streams,'close'):
            streams.close()
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':
    raise SystemExit(main())

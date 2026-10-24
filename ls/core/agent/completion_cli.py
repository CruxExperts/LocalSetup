"""Protected tool-free completion command with bounded JSON-only results."""
import argparse
import os
from pathlib import Path
import select
import signal
import stat
import sys
import threading
import time
import uuid

from .broker_rpc import _encode
from .completion_contract import MAX_REQUEST,EXITS,envelope
from .profiles import load,wire
from .run_io import Streams

CREDENTIAL='LOCALSETUP_COMPLETION_CREDENTIAL'
PROFILE='LOCALSETUP_COMPLETION_PROFILE'


class Parser(argparse.ArgumentParser):
    def error(self,message):raise ValueError('Invalid completion arguments')


def configure(complete):
    from .diagnostics import locations
    locations=locations(Path.home())
    complete.add_argument('--profile',required=True)
    complete.add_argument('--request',required=True,help='JSON request file or - for stdin')
    complete.add_argument('--profiles',type=Path,default=Path(locations['profiles']))
    complete.add_argument('--runtime-root',type=Path)
    complete.add_argument('--timeout',type=float,default=120,help='Overall execution cap in seconds, at most 3600')
    complete.add_argument('--context',action='append',default=[],help='Granted workspace-relative UTF-8 file (repeat up to eight times)')
    complete.add_argument('--grant',type=Path,help='Private read/disclosure grant required for context files')
    complete.add_argument('--workspace',type=Path,default=Path.cwd())


def arguments(argv, *, default_runtime_root=None):
    from ..branding import FRAMEWORK_COMMAND
    from .diagnostics import locations
    parser=Parser(prog=FRAMEWORK_COMMAND+' llm')
    sub=parser.add_subparsers(dest='action',required=True)
    configure(sub.add_parser('complete',help='Make one bounded tool-free model request'))
    args=parser.parse_args(argv)
    args.runtime_root=args.runtime_root or default_runtime_root or Path(locations(Path.home())['runtimes'])
    if bool(args.context) != (args.grant is not None):
        raise ValueError('Context files require an explicit grant')
    if args.context:
        from .completion_context import validate_paths
        validate_paths(args.context)
    return args


def emit(result):
    try:Streams(time.monotonic()+1,threading.Event()).write(_encode(result).decode()+'\n')
    except (OSError,ValueError):return EXITS['transport_failed']
    return EXITS[result['status']]


def read_request(path,expires,cancelled):
    fd=0;owned=False
    try:
        if path!='-':
            fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW);owned=True
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('Request file must be regular')
        data=bytearray()
        while True:
            if cancelled.is_set():raise InterruptedError('Cancelled')
            if time.monotonic()>=expires:raise TimeoutError('Request input deadline')
            if not select.select([fd],[],[],min(0.05,max(0,expires-time.monotonic())))[0]:continue
            raw=os.read(fd,min(4096,MAX_REQUEST+1-len(data)))
            if not raw:break
            data.extend(raw)
            if len(data)>MAX_REQUEST:raise ValueError('Request exceeds byte limit')
        return bytes(data).decode('utf-8')
    finally:
        if owned:os.close(fd)


def main(argv=None,*,protected=False,default_runtime_root=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    model=None;cancelled=threading.Event();previous={};dispatched=False
    try:
        args=arguments(argv,default_runtime_root=default_runtime_root)
        import math
        if not math.isfinite(args.timeout) or not 0<args.timeout<=3600:raise ValueError('Invalid timeout')
        expires=time.monotonic()+args.timeout
        if not protected:
            from .runtime_install import selected
            try:
                profile=load(args.profiles,args.profile);model=profile.model
                from .profile_credentials import resolve
                credential=resolve(profile)
                with selected(args.runtime_root,timeout=min(5,args.timeout)) as release:executable=release/'venv/bin/python'
            except (OSError,ValueError,RuntimeError):return emit(envelope('unavailable',model=model))
            from .coding_protocol import profile_digest
            remaining=expires-time.monotonic()
            if remaining<=0:raise TimeoutError('Bootstrap deadline')
            os.execve(executable,[str(executable),'-I','-B','-m','ls.core.agent.completion_cli',*argv,
                '--profiles',str(args.profiles.absolute()),'--runtime-root',str(args.runtime_root.absolute()),'--timeout',str(remaining)],
                {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',CREDENTIAL:credential,PROFILE:profile_digest(wire(profile))})
        if not sys.flags.isolated or not sys.dont_write_bytecode or not Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
            return emit(envelope('unavailable'))
        previous={sig:signal.signal(sig,lambda *_:cancelled.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
        profile=load(args.profiles,args.profile);model=profile.model
        from .coding_protocol import profile_digest
        if profile_digest(wire(profile))!=os.environ.get(PROFILE):return emit(envelope('unavailable',model=model))
        raw=read_request(args.request,expires,cancelled)
        if args.context:
            from .completion_context import attach
            raw=attach(raw,args.context,args.grant,args.workspace,expires,cancelled)
        payload={'profile':wire(profile),'credential':os.environ.get(CREDENTIAL,''),'request':raw}
        from .completion_run import run,identity
        from .coding_run import CodingGrant
        identifier=uuid.uuid4().hex
        authority=CodingGrant(identifier,identifier,identity(payload),expires,revoked=cancelled)
        dispatched=True
        result=run(args.runtime_root,payload,authority,expected_release=Path(sys.prefix).parent)
        if cancelled.is_set():return emit(envelope('cancelled',model=model,attempts=1))
        return emit(result)
    except (OSError,ValueError,TypeError,RecursionError,RuntimeError) as error:
        status='cancelled' if cancelled.is_set() else 'deadline' if isinstance(error,TimeoutError) else 'uncertain' if dispatched else 'invalid_request'
        return emit(envelope(status,model=model,attempts=int(dispatched)))
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':raise SystemExit(main(protected=True))

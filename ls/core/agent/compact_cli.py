"""Explicit protected compaction command and task-bound history disclosure."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

from ..branding import CLI_COMMAND
from .profiles import load,wire

CREDENTIAL='LOCALSETUP_COMPACT_CREDENTIAL'
PROFILE='LOCALSETUP_COMPACT_PROFILE'


def arguments(parser):
    for name in ('profile','checkpoint','task','session'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--disclose-history',action='store_true',required=True,help='Authorize this checkpoint history for the selected provider')
    parser.add_argument('--profiles',type=Path)
    parser.add_argument('--workspace',type=Path,default=Path.cwd())
    parser.add_argument('--runtime-root',type=Path)
    parser.add_argument('--state-root',type=Path)
    parser.add_argument('--keep-messages',type=int,default=8)
    parser.add_argument('--token-limit',type=int,default=32768)
    parser.add_argument('--timeout',type=float,default=120)


def defaults(args):
    from .diagnostics import locations
    found=locations(Path.home())
    args.profiles=(args.profiles or Path(found['profiles'])).absolute()
    args.state_root=(args.state_root or Path(found['state'])).absolute()
    args.runtime_root=(args.runtime_root or Path(found['runtimes'])).absolute()
    args.workspace=args.workspace.absolute()
    return args


def launch(argv,args):
    from .runtime_install import selected
    from .coding_protocol import profile_digest
    args=defaults(args);profile=load(args.profiles,args.profile)
    from .profile_credentials import resolve
    credential=resolve(profile)
    with selected(args.runtime_root,timeout=5) as release:executable=release/'venv/bin/python'
    os.execve(executable,[str(executable),'-I','-B','-m','ls.core.agent.compact_cli',*argv],
              {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',CREDENTIAL:credential,PROFILE:profile_digest(wire(profile))})


def main(argv=None):
    from .run_cli import failure
    parser=argparse.ArgumentParser(prog=CLI_COMMAND+' compact');arguments(parser)
    args=defaults(parser.parse_args(argv))
    if not sys.flags.isolated or not sys.dont_write_bytecode or not Path(__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        return failure('text',0,'failed',3,'compaction requires its protected installed runtime.')
    if not math.isfinite(args.timeout) or not 0<args.timeout<=3600:
        return failure('text',0,'failed',2,'compaction timeout must be within 3600 seconds.')
    expires=time.monotonic()+args.timeout;cancelled=threading.Event()
    previous={sig:signal.signal(sig,lambda *_:cancelled.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        from .session_owner import lease,_separate
        from .coding_run import CodingGrant
        from .coding_protocol import profile_digest
        from .compaction_run import compact_checkpoint,request
        from .run_io import Streams
        for path in (args.profiles,args.state_root,args.runtime_root):_separate(path,args.workspace)
        _separate(args.state_root,args.runtime_root)
        profile=load(args.profiles,args.profile);raw=wire(profile)
        if profile_digest(raw)!=os.environ.get(PROFILE):
            raise ValueError('Provider profile changed across protected startup')
        with lease(args.state_root/'sessions',task=args.task,session=args.session,workspace=args.workspace,expires=expires,revoked=cancelled,create=False) as owner:
            history=owner.resume_checkpoint(args.checkpoint,profile=profile_digest(raw))
            payload={'schema_version':1,'profile':raw,'credential':profile.credential({profile.credential_env:os.environ.get(CREDENTIAL,'')}),
                     'history':history.decode(),'keep_messages':args.keep_messages,'token_limit':args.token_limit}
            authority=CodingGrant(args.task,args.session,request(payload),expires,revoked=cancelled)
            result=compact_checkpoint(owner,args.runtime_root,args.checkpoint,payload,authority,expected_release=Path(sys.prefix).parent)
        Streams(time.monotonic()+1,threading.Event()).write(json.dumps(result,ensure_ascii=True)+'\n')
        return 0
    except (OSError,ValueError,TypeError,RecursionError,RuntimeError):
        code=130 if cancelled.is_set() else 124 if time.monotonic()>=expires else 2
        return failure('text',0,'failed',code,'compaction failed; inspect retained checkpoint evidence before retrying.')
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)


if __name__=='__main__':raise SystemExit(main())

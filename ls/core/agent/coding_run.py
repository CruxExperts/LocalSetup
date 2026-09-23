"""Supervisor-owned coding execution with explicit disclosure and file authority."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import socket
import threading
import time

from .broker_rpc import Channel, _encode
from .coding_protocol import CodingHandler, METHODS, profile_digest, request, terminal
from .file_broker import FileBroker
from .operation_journal import DIGEST, IDENTIFIER
from .process_rpc import ProcessHandler
from .resource_group import Limits
from .session_owner import lease, _Revocation, _separate
from .supervisor import Outcome, supervise
from .tool_preflight import qualified_tools


def disclosure_digest(payload):
    request(payload)
    return hashlib.sha256(_encode({key:value for key,value in payload.items() if key!='credential'})).hexdigest()


@dataclass(frozen=True)
class CodingGrant:
    task: str
    session: str
    request_sha256: str
    expires: float
    revoked: threading.Event = field(default_factory=threading.Event,repr=False,compare=False)

    def __post_init__(self):
        if any(not isinstance(x,str) or not IDENTIFIER.fullmatch(x) for x in (self.task,self.session)) or not isinstance(self.request_sha256,str) or not DIGEST.fullmatch(self.request_sha256) or not math.isfinite(self.expires):
            raise ValueError('Coding disclosure grant requires explicit identity, request digest and deadline')

    def check(self, digest):
        if digest!=self.request_sha256 or self.revoked.is_set() or time.monotonic()>=self.expires:
            raise PermissionError('Coding context disclosure is mismatched, revoked or expired')


@dataclass(frozen=True)
class RunPaths:
    runtimes: Path
    sessions: Path
    target_leases: Path
    snapshots: Path
    scratch: Path
    resource_parent: Path

    def validate(self,workspace):
        paths=(self.runtimes,self.sessions,self.target_leases,self.snapshots,self.scratch,self.resource_parent)
        if any(not isinstance(p,Path) or not p.is_absolute() for p in paths):
            raise ValueError('Coding paths must be explicit absolute paths')
        for index,path in enumerate(paths):
            for other in (*paths[index+1:],workspace,Path('/usr')):
                _separate(path,other)
        _separate(workspace,Path('/usr'))


def run_coding(paths: RunPaths, payload: dict, authority: CodingGrant, files, recipes: dict,
               *, limits: Limits, on_event, resume=None, cancel=None, expected_release=None, steering=None, approve=None, new_session=False, openpgp=None) -> Outcome:
    """Caller authorizes exact context; saved messages never restore authority."""
    if type(new_session) is not bool or (new_session and (resume is not None or payload.get('history') is not None)):
        raise ValueError('Atomic new session requires fresh history')
    payload=json.loads(_encode(payload))
    digest=disclosure_digest(payload)
    authority.check(digest)
    if (files.task,files.session)!=(authority.task,authority.session):
        raise PermissionError('Coding file authority belongs to another task/session')
    paths.validate(files.root)
    expires=min(authority.expires,files.expires)
    revoked=_Revocation(authority.revoked,files.revoked,*(() if cancel is None else (cancel,)))
    def current():
        authority.check(digest)
        if revoked.is_set() or time.monotonic()>=expires:
            raise PermissionError('Coding run is revoked or expired')
    try:
        current()
        with lease(paths.sessions,task=authority.task,session=authority.session,workspace=files.root,
                   expires=expires,revoked=revoked,new=new_session) as owner:
            with owner._operation():pass
            if resume is None:
                if payload['history'] is not None:
                    raise PermissionError('Restored history requires an explicit current checkpoint')
            else:
                history=owner.resume_checkpoint(resume,profile=profile_digest(payload['profile']))
                if payload['history'] is None or payload['history'].encode()!=history:
                    raise PermissionError('Restored history differs from the current checkpoint')
            with qualified_tools(paths.runtimes,paths.scratch,paths.resource_parent,task=authority.task,
                    session=authority.session,expires=expires,limits=limits,revoked=revoked) as qualification:
                if expected_release is not None and qualification.release != expected_release:
                    raise PermissionError('Selected runtime changed; restart the protected command')
                broker=FileBroker(replace(files,expires=expires,revoked=revoked),paths.target_leases)
                tools=ProcessHandler(owner,broker,profile=profile_digest(payload['profile']),run_id=payload['run_id'],
                    runtimes=paths.runtimes,snapshots=paths.snapshots,recipes=recipes,openpgp=openpgp,
                    resource_parent=paths.resource_parent,limits=limits)
                def check():
                    current();qualification.check(authority.task,authority.session);owner._check()
                handler=CodingHandler(tools,payload,on_event,check,steering=steering,approve=approve)
                left,right=socket.socketpair()
                channel=None
                try:
                    channel=Channel(left,task=authority.task,session=authority.session,methods=METHODS,
                                    expires=expires,cancelled=revoked)
                    def active_worker():
                        check()
                        channel._check()
                    handler.check=active_worker
                    check()
                    outcome=supervise([str(qualification.release/'venv/bin/python'),'-I','-B','-m',
                        'ls.core.agent.coding_worker',str(right.fileno()),authority.task,authority.session,str(expires)],
                        b'',cwd=qualification.release,environment={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                        timeout=max(0,expires-time.monotonic()),cancel=revoked,capture=True,
                        pass_fds=(right.fileno(),),broker=(channel,handler,check))
                    if outcome.status!='completed':
                        return Outcome(outcome.status,outcome.returncode)
                    result=terminal(outcome.data['stdout'].encode(),handler.finished)
                    owner.resume_checkpoint(result['checkpoint'],profile=tools.profile)
                    check()
                finally:
                    if channel is not None:channel.close()
                    else:left.close()
                    right.close()
        current()
        return Outcome('completed',outcome.returncode,result)
    except Exception:
        if revoked.is_set():return Outcome('cancelled',None)
        if time.monotonic()>=expires:return Outcome('timed_out',None)
        raise

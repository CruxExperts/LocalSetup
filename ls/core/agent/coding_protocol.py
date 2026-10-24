"""Bounded coding-worker exchange; credentials travel only over inherited RPC."""
from __future__ import annotations

import hashlib
import json

from .broker_rpc import _encode
from .checkpoint_store import MAX_MESSAGES
from .checkpoint_rpc import METHOD as CHECKPOINT_METHOD
from .operation_journal import DIGEST, IDENTIFIER
from .profiles import parse
from .process_rpc import METHODS as TOOL_METHODS

METHODS=TOOL_METHODS|{'run.start','stream.event','run.finish','run.steering',
                      'provider.open','provider.next','provider.close'}
MAX_STREAM=1024*1024
MODEL_TOOL_METHODS = TOOL_METHODS - {CHECKPOINT_METHOD}


def request(value, *, worker=False):
    keys={'schema_version','run_id','profile','credential','prompt','instructions','history','request_limit','tool_limit','token_limit'}
    if worker:
        keys.remove('credential')
    if not isinstance(value,dict) or set(value) not in (keys,keys|{'images'}) or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('Unsupported coding request schema')
    if not isinstance(value['run_id'],str) or not IDENTIFIER.fullmatch(value['run_id']):
        raise ValueError('Coding request requires a bounded run identity')
    for name in ('prompt','instructions'):
        if not isinstance(value[name],str) or not value[name] or len(value[name].encode())>128*1024:
            raise ValueError('Coding context exceeds limit')
    if value['history'] is not None and (not isinstance(value['history'],str) or len(value['history'].encode())>MAX_MESSAGES):
        raise ValueError('Coding history exceeds limit')
    for name,low,high in (('request_limit',1,64),('tool_limit',0,256),('token_limit',1,1024*1024)):
        if type(value[name]) is not int or not low<=value[name]<=high:
            raise ValueError('Coding usage limits require bounded integers')
    profile=parse(value['profile'])
    if 'images' in value:
        from .image_inputs import validate
        validate(value['images'])
        if value['images'] and 'images' not in profile.capabilities:
            raise ValueError('Coding profile does not declare image support')
    if not {'tools','streaming'}<=profile.capabilities:
        raise ValueError('Coding profile requires explicit tool and streaming capabilities')
    if not worker:
        if not isinstance(value['credential'],str) or len(value['credential'])>4096:
            raise ValueError('Coding credential exceeds limit')
        profile.credential({profile.credential_env:value['credential']})
    if len(_encode(value))>12*1024*1024:
        raise ValueError('Coding request exceeds transport budget')
    return profile


def profile_digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


class CodingHandler:
    """Trusted controller supplies context authority, preflight and event sink."""
    def __init__(self, tools, payload, on_event, check, steering=None, approve=None,
                 *, provider_task, provider_session, provider_deadline,
                 provider_resource_parent, provider_limits):
        profile=request(payload)
        if tools.profile!=profile_digest(payload['profile']) or tools.run_id!=payload['run_id']:
            raise PermissionError('Coding request and broker profile/run must match')
        if not callable(on_event) or not callable(check):
            raise ValueError('Coding handler requires explicit event and authority callbacks')
        self.tools,self.payload=tools,json.loads(_encode(payload))
        self.on_event,self.check=on_event,check
        self.steering = steering
        self.approve = approve
        from .provider_client import SupervisedProvider
        self.provider = SupervisedProvider(profile,payload['credential'],payload['token_limit'],
                                           task=provider_task,session=provider_session,
                                           check=lambda:self.check(),deadline=provider_deadline,
                                           request_limit=payload['request_limit'],
                                           resource_parent=provider_resource_parent,
                                           limits=provider_limits)
        self.started,self.finished=False,None
        self.bytes,self.events=0,0
        self.tool_calls = 0

    def __call__(self, method, data):
        self.check()
        if self.finished is not None:
            raise ValueError('Coding worker already reported its result')
        if method=='run.start':
            if self.started or data!={}:
                raise ValueError('Coding worker may start exactly once')
            self.started=True
            return {key:value for key,value in self.payload.items() if key!='credential'}
        if not self.started:
            raise ValueError('Coding worker must start before dispatch')
        if method in ('provider.open','provider.next','provider.close'):
            return self.provider(method,data)
        if method=='run.steering':
            if data != {}:
                raise ValueError('Invalid steering poll')
            messages = [] if self.steering is None else self.steering()
            if (not isinstance(messages,list) or len(messages)>32
                    or any(not isinstance(x,str) or not x or len(x.encode())>8192 for x in messages)
                    or len(_encode(messages))>256*1024):
                raise ValueError('Invalid steering payload')
            self.check()
            return {'messages':messages}
        if method=='stream.event':
            if not isinstance(data,dict) or set(data)!={'event'} or not isinstance(data['event'],dict):
                raise ValueError('Invalid coding stream event')
            raw=_encode(data['event'])
            if self.bytes+len(raw)>MAX_STREAM or self.events>=10000:
                raise ValueError('Coding stream budget exceeded')
            self.bytes+=len(raw);self.events+=1
            self.on_event(data['event'])
            self.check()
            return {'accepted':self.events}
        if method=='run.finish':
            self.provider.close()
            if not isinstance(data,dict) or set(data)!={'output','checkpoint','usage'}:
                raise ValueError('Invalid coding result fields')
            if not isinstance(data['output'],str) or len(data['output'].encode())>MAX_STREAM or not isinstance(data['checkpoint'],str) or not DIGEST.fullmatch(data['checkpoint']):
                raise ValueError('Invalid coding result bounds or checkpoint')
            if not isinstance(data['usage'],dict) or len(_encode(data['usage']))>4096:
                raise ValueError('Coding usage report exceeds limit')
            checkpoint=self.tools.owner._checkpoint(data['checkpoint'])
            if checkpoint['run_id']!=self.tools.run_id:
                raise PermissionError('Coding result checkpoint belongs to another run')
            self.tools.owner.resume_checkpoint(data['checkpoint'],profile=self.tools.profile)
            self.check()
            self.finished=json.loads(_encode(data))
            return {'checkpoint':data['checkpoint']}
        if method in MODEL_TOOL_METHODS:
            # Worker-side SDK limits are not supervisor authority. Charge each
            # attempted model tool before approval or broker effects; checkpoint
            # persistence remains available even when no model tools are allowed.
            if self.tool_calls >= self.payload['tool_limit']:
                raise PermissionError('Coding tool budget exhausted')
            self.tool_calls += 1
        if self.approve is not None and method in MODEL_TOOL_METHODS:
            data=self.approve(method,data,self.tools.recipes,self.check)
            self.check()
        return self.tools(method,data)


def terminal(raw, expected):
    from .broker_rpc import _decode
    if len(raw)>4096 or expected is None:
        raise ValueError('Coding worker did not acknowledge a result')
    value=_decode(raw)
    if value!={'schema_version':1,'status':'completed','checkpoint':expected['checkpoint']} or type(value.get('schema_version')) is not int:
        raise ValueError('Coding process receipt disagrees with acknowledged result')
    return expected

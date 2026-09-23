"""Named supervisor-owned process recipes projected from fresh file grants."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import time
from types import MappingProxyType

from .file_grants import relative
from .file_rpc import FileHandler, METHODS as FILE_METHODS
from .operation_journal import IDENTIFIER
from .process_broker import run_recorded
from .sandbox import ProcessGrant
from .resource_group import Limits
from .session_owner import _separate
from .snapshot import create

METHODS = FILE_METHODS | {'process.run'}


@dataclass(frozen=True)
class Recipe:
    command: tuple[str, ...]
    files: tuple[str, ...]
    seconds: float

    def __post_init__(self):
        ProcessGrant('recipe','recipe',Path('/'),self.command,1)
        if not isinstance(self.files, tuple) or not self.files or len(self.files)>30000 or len(set(self.files))!=len(self.files):
            raise ValueError('Recipe requires a bounded immutable input inventory')
        for name in self.files:
            relative(name)
        if isinstance(self.seconds,bool) or not math.isfinite(self.seconds) or self.seconds<=0:
            raise ValueError('Recipe requires a positive finite time limit')


class ProcessHandler(FileHandler):
    def __init__(self, owner, broker, *, profile, run_id, runtimes, snapshots, recipes, resource_parent=None, limits=None, openpgp=None):
        super().__init__(owner,broker,profile=profile,run_id=run_id,openpgp=openpgp)
        if not isinstance(recipes,dict) or len(recipes)>64 or any(
            not isinstance(name,str) or not IDENTIFIER.fullmatch(name) or not isinstance(value,Recipe)
            for name,value in recipes.items()):
            raise ValueError('Process handler requires bounded explicit named recipes')
        if limits is not None and not isinstance(limits,Limits):
            raise ValueError('Process handler requires explicit resource limits')
        self.resource_parent=resource_parent
        self.limits=limits if limits is not None else Limits()
        self.recipes=MappingProxyType(dict(recipes))
        self.runtimes,self.snapshots=Path(runtimes).absolute(),Path(snapshots).absolute()

    def __call__(self, method, data):
        if method!='process.run':
            return super().__call__(method,data)
        if not isinstance(data,dict) or set(data)!={'name','checkpoint','call_id'}:
            raise ValueError('Unsupported process RPC schema')
        if not isinstance(data['name'],str) or data['name'] not in self.recipes:
            raise PermissionError('Process recipe is not granted')
        if not isinstance(data['call_id'],str) or not IDENTIFIER.fullmatch(data['call_id']):
            raise ValueError('Process requires a bounded SDK tool call identity')
        recipe=self.recipes[data['name']]
        owner=self.owner
        with owner._operation() as operations:
            checkpoint=owner._checkpoint(data['checkpoint'])
            if checkpoint['run_id']!=self.run_id or checkpoint['profile']!=self.profile:
                raise PermissionError('Process requires a matching run/profile checkpoint')
            if any(value['intent'].get('tool_call',{}).get('run_id')==self.run_id and
                   value['intent'].get('tool_call',{}).get('call_id')==data['call_id'] for value in operations.values()):
                raise ValueError('Tool call already has an operation; reconcile without replay')
            for boundary in (self.runtimes,self.snapshots,Path('/usr')):
                _separate(owner.root.parent,boundary)
            _separate(self.snapshots,self.runtimes)
            _separate(self.snapshots,Path('/usr'))
            broker=owner._broker(self.broker)
            for boundary in (broker.grant.root,broker.lease_root,self.runtimes):
                _separate(boundary,Path('/usr'))
            expires=min(owner.expires,broker.grant.expires,time.monotonic()+recipe.seconds)
            broker=type(broker)(replace(broker.grant,expires=expires),broker.lease_root)
            snapshot=create(broker,self.snapshots,recipe.files,task=owner._journal.task,
                            session=owner._journal.session,for_provider=True)
            grant=replace(snapshot.process(recipe.command,expires=expires),
                          resource_parent=self.resource_parent,limits=self.limits)
            manifest=hashlib.sha256(snapshot.manifest.read_bytes()).hexdigest()
            arguments={'name':data['name'],'command':recipe.command,'files':recipe.files,'seconds':recipe.seconds}
            call={'run_id':self.run_id,'call_id':data['call_id'],'name':'run_command',
                  'arguments_sha256':hashlib.sha256(json.dumps(arguments,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
            outcome=run_recorded(self.runtimes,grant,owner._journal,snapshot_sha256=manifest,
                                 task=owner._journal.task,session=owner._journal.session,provider=True,
                                 checkpoint=data['checkpoint'],tool_call=call)
            operations=owner._journal.inspect(timeout=max(0,owner.expires-time.monotonic()))
            operation=next(key for key,value in operations.items() if value['intent'].get('tool_call')==call)
            result={'operation':operation,'status':outcome.status,'returncode':outcome.returncode,'output':outcome.data}
            if outcome.data is not None:
                grant.check(owner._journal.task,owner._journal.session)
            from .tool_results import save
            save(owner, result, profile=self.profile, checkpoint=data['checkpoint'], tool_call=call)
            if outcome.data is not None:
                grant.check(owner._journal.task,owner._journal.session)
            owner._check()
            return result

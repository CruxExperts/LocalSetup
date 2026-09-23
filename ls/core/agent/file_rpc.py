"""Supervisor file RPC binds model data to fresh grants and durable call identity."""
from __future__ import annotations

import hashlib
import json

from .checkpoint_rpc import CheckpointHandler, METHOD
from .file_broker import MAX_FILE
from .operation_journal import IDENTIFIER

METHODS = frozenset({METHOD, 'file.read', 'file.write', 'file.search', 'file.list', 'context.refresh'})


class FileHandler(CheckpointHandler):
    def __init__(self, owner, broker, *, profile, run_id, openpgp=None):
        super().__init__(owner, profile=profile, run_id=run_id)
        self.broker = broker
        self.openpgp = {} if openpgp is None else dict(openpgp)

    def __call__(self, method, data):
        if method == METHOD:
            return super().__call__(method, data)
        if method == 'context.refresh':
            if not isinstance(data,dict) or set(data)!={'directory'}:
                raise ValueError('Invalid context refresh schema')
            from .nested_context import refresh
            return refresh(self.owner,self.broker,data['directory'])
        if method == 'file.list':
            if not isinstance(data,dict) or set(data)!={'path'}:
                raise ValueError('Invalid listing schema')
            from .file_listing import listing
            return listing(self.owner,self.broker,data['path'])
        if method == 'file.search':
            from .file_search import search
            return search(self.owner,self.broker,data)
        if method == 'file.read' and isinstance(data, dict) and set(data) == {'path', 'cursor'}:
            return self.owner.read_page(self.broker, data['path'], cursor=data['cursor'],
                                        for_provider=True, **self.openpgp)
        required = {'path','content','expected_before','checkpoint','call_id'}
        if method != 'file.write' or not isinstance(data, dict) or set(data) != required:
            raise ValueError('Unsupported file RPC method or schema')
        if not isinstance(data['content'], str) or len(data['content'].encode()) > MAX_FILE:
            raise ValueError('File RPC replacement exceeds text limit')
        if not isinstance(data['call_id'], str) or not IDENTIFIER.fullmatch(data['call_id']):
            raise ValueError('File RPC requires a bounded SDK tool call identity')
        arguments = {key:data[key] for key in ('path','content','expected_before')}
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        call = {'run_id':self.run_id,'call_id':data['call_id'],'name':'write_file','arguments_sha256':digest}
        operation = self.owner.write(self.broker, data['path'], data['content'].encode(),
                                     expected_before=data['expected_before'], checkpoint=data['checkpoint'],
                                     tool_call=call, profile=self.profile)
        result = {'operation':operation,'status':'applied'}
        from .tool_results import save
        with self.owner._operation():
            save(self.owner, result, profile=self.profile, checkpoint=data['checkpoint'], tool_call=call)
        return result

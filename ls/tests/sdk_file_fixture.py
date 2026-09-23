"""Deterministic SDK file RPC fixture; no live provider."""
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sys
import time

if len(sys.argv)==3:
    sys.path.insert(0,sys.argv[2]);payload=Path(sys.argv[2])/'vendor/lscli'
else:
    import ls
    payload=Path(ls.__file__).parent/'_sdk_payload'
from ls.core.agent.sdk_imports import activate
finder=activate(payload)
from ls.core.agent.broker_rpc import Channel
from ls.core.agent.file_rpc import METHODS
from ls.core.agent.sdk_file_tools import file_tools
from ls.core.agent.sdk_persistence import checkpoint_store
from ls.core.agent.sdk_iteration import iterate
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.messages import ToolReturnPart
channel=Channel(socket.socket(fileno=int(sys.argv[1])),task='task',session='session',methods=METHODS,expires=time.monotonic()+10)


async def main():
    turns=0
    async def stream(messages,info):
        nonlocal turns
        turns+=1
        if turns==1:
            yield {0:DeltaToolCall(name='read_file',json_args=json.dumps({'path':'src/a.txt'}),tool_call_id='read-1')}
        elif turns==2:
            returned=[part for message in messages for part in message.parts if isinstance(part,ToolReturnPart)][-1].content
            assert returned['content']=='original' and returned['encoding']=='utf-8'
            assert returned['next_cursor'] is None and returned['bytes']==len(b'original')
            yield {0:DeltaToolCall(name='write_file',json_args=json.dumps({'path':'src/a.txt','content':'changed','expected_before':hashlib.sha256(returned['content'].encode()).hexdigest()}),tool_call_id='write-1')}
        else:
            yield 'file updated'
    store=checkpoint_store(finder,channel,run_id='run')
    async def emit(raw):pass
    result=await iterate(FunctionModel(stream_function=stream),finder,prompt='Read and update fixture.',instructions='Use only granted files.',
                         tools=file_tools(finder,channel),store=store,on_event=emit,check=lambda:None,
                         expires=time.monotonic()+10,run_id='run',conversation_id='conversation')
    channel.close()
    print(json.dumps({'output':result['output'],'checkpoint':store.last_checkpoint,'messages':result['messages'].decode(),
                      'origins':finder.verify_origins()}))
asyncio.run(main())

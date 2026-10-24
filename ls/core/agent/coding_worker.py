"""Installed isolated SDK coding worker; supervisor owns all tool authority."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys

from .broker_rpc import Channel
from .coding_protocol import METHODS, request
from .sdk_imports import activate


async def run(channel,finder):
    payload=await channel.request_async('run.start',{})
    profile=request(payload,worker=True)
    from .sdk_models import model
    from .sdk_iteration import iterate
    from .sdk_persistence import checkpoint_store
    from .sdk_file_tools import file_tools
    from .sdk_process_tool import process_tool
    from .provider_client import BrokerTransport
    store=checkpoint_store(finder,channel,run_id=payload['run_id'])
    events=0
    async def emit(raw):
        nonlocal events
        events+=1
        ack=await channel.request_async('stream.event',{'event':json.loads(raw)})
        if ack!={'accepted':events} or type(ack.get('accepted')) is not int:
            raise ValueError('Invalid coding stream acknowledgement')
    async def steering():
        value=await channel.request_async('run.steering',{})
        if not isinstance(value,dict) or set(value)!={'messages'}:
            raise ValueError('Invalid steering response')
        return value['messages']
    from .image_inputs import validate as image_bytes
    from pydantic_ai.messages import BinaryContent
    images=tuple(BinaryContent(data=data,media_type=mime) for data,mime in image_bytes(payload.get('images',[])))
    async with model(profile,{profile.credential_env:'brokered-placeholder'},finder,
                     transport=BrokerTransport(channel,profile)) as adapter:
        result=await iterate(adapter,finder,prompt=payload['prompt'],instructions=payload['instructions'],
            tools=(*file_tools(finder,channel),process_tool(finder,channel)),store=store,on_event=emit,check=channel._check,
            expires=channel.expires,run_id=payload['run_id'],conversation_id=channel.session,
            history=None if payload['history'] is None else payload['history'].encode(),
            request_limit=payload['request_limit'],tool_limit=payload['tool_limit'],token_limit=payload['token_limit'],steering=steering,images=images)
    checkpoint=store.last_checkpoint
    ack=await channel.request_async('run.finish',{'output':result['output'],'checkpoint':checkpoint,'usage':result['usage']})
    if ack!={'checkpoint':checkpoint}:
        raise ValueError('Invalid coding terminal acknowledgement')
    finder.verify_origins()
    return {'schema_version':1,'status':'completed','checkpoint':checkpoint}


def main():
    channel=None
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode or len(sys.argv)!=5:
            raise ValueError('Coding worker requires isolated execution and inherited channel')
        package=Path(__file__).resolve().parents[2]
        if not package.is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError('Coding worker requires its installed runtime')
        channel=Channel(socket.socket(fileno=int(sys.argv[1])),task=sys.argv[2],session=sys.argv[3],
                        methods=METHODS,expires=float(sys.argv[4]))
        finder=activate(package/'_sdk_payload')
        print(json.dumps(asyncio.run(run(channel,finder))))
        return 0
    except Exception:
        # SDK/provider errors may contain credential or request content.
        print('Coding worker failed; inspect supervisor outcome and journal.',file=sys.stderr)
        return 2
    finally:
        if channel is not None:channel.close()


if __name__=='__main__':
    raise SystemExit(main())

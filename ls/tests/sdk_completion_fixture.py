"""Isolated direct model completion with synthetic protocol responses."""
import asyncio,json,sys,time
from pathlib import Path
if len(sys.argv)>1:
    sys.path.insert(0,sys.argv[1]);payload=Path(sys.argv[1])/'./vendor/lscli'
else:
    import ls
    payload=Path(ls.__file__).parent/'_sdk_payload'
from ls.core.agent.sdk_imports import activate
finder=activate(payload)
from ls.core.agent.sdk_completion import complete
import ls.core.agent.sdk_completion as completion
base_validate=completion.validate_output
from ls.core.agent.profiles import parse
from ls.core.branding import user_agent
import httpx2 as httpx

async def main():
    # Allow cold SDK imports on hosted runners; the deadline case overrides this.
    reports=[]
    from dataclasses import replace
    from ls.core.agent.profiles import REASONING_EFFORTS
    for api in ('chat_completions','responses'):
        profile=parse({'base_url':'https://fixture.invalid/v1/','api':api,'model':'fixture','credential_env':'KEY','timeout_seconds':5,'capabilities':['native_schema'],'allow_loopback_http':False})
        request=json.dumps({'interface_version':1,'model':'fixture','deadline_seconds':30,'max_attempts':1,'max_output_tokens':100,'input':{'facts':[]},'output_schema':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}}).encode()
        for mode,expected in [('success','succeeded'),('refusal','refused'),('incomplete','incomplete'),('malformed','malformed'),('schema','schema_rejected'),('rate','rate_limited'),('error','provider_error'),('missing','unavailable'),('connect','transport_failed'),('read','uncertain'),('large','output_limit'),('revoke','cancelled'),('deadline','deadline'),('options','succeeded'),('validate_only','succeeded'),('validate_only_schema','schema_rejected')]+([('split_text','succeeded'),('item_incomplete','incomplete'),('wrong_role','malformed')] if api=='responses' else [])+[(effort,'succeeded') for effort in sorted(REASONING_EFFORTS)]:
            calls=[];revoked=False
            def current():
                if revoked:raise PermissionError('revoked')
            def validate(text,request):
                nonlocal revoked
                result=base_validate(text,request)
                if mode=='revoke':revoked=True
                if mode=='deadline':time.sleep(0.3)
                return result
            completion.validate_output=validate
            actual_request=request;actual_profile=profile
            if mode.startswith('validate_only'):
                value=json.loads(request);value['schema_mode']='validate_only'
                actual_request=json.dumps(value).encode();actual_profile=replace(profile,capabilities=frozenset())
            if mode=='options':
                value=json.loads(request);value.update(temperature=0.25,schema_name='qc_fixture',session_id='a'*32)
                actual_request=json.dumps(value).encode()
                actual_profile=replace(profile,capabilities=profile.capabilities | {'temperature'},organization='org-fixture',project='proj-fixture')
            if mode in REASONING_EFFORTS:
                value=json.loads(request);value['reasoning_effort']=mode
                actual_request=json.dumps(value).encode()
                try:await complete(profile,{'KEY':'fixture'},finder,actual_request,expires=time.monotonic()+30,check=lambda:None,transport=httpx.MockTransport(lambda wire: (_ for _ in ()).throw(AssertionError('Undeclared effort dispatched'))))
                except ValueError:pass
                else:raise AssertionError('Undeclared reasoning accepted')
                actual_profile=replace(profile,capabilities=profile.capabilities | {'reasoning:'+mode})
            if mode=='deadline':
                value=json.loads(request);value['deadline_seconds']=0.2
                actual_request=json.dumps(value).encode()
            def receive(wire):
                calls.append(wire);body=json.loads(wire.content)
                assert wire.headers['user-agent']==user_agent() and not body.get('tools') and not body.get('stream')
                assert wire.headers['x-omniroute-compression']=='off'
                assert wire.headers.get('x-session-id')==('a'*32 if mode=='options' else None)
                assert 'session_id' not in body
                if mode=='options':
                    assert body['temperature']==0.25 and wire.headers['OpenAI-Organization']=='org-fixture' and wire.headers['OpenAI-Project']=='proj-fixture'
                else:assert 'temperature' not in body and 'OpenAI-Organization' not in wire.headers and 'OpenAI-Project' not in wire.headers
                if mode in REASONING_EFFORTS:
                    assert (body.get('reasoning_effort') if api=='chat_completions' else body.get('reasoning',{}).get('effort'))==mode
                else:assert 'reasoning_effort' not in body and not body.get('reasoning')
                if mode=='connect':raise httpx.ConnectError('private connect')
                if mode=='read':raise httpx.ReadError('private read')
                if mode=='large':return httpx.Response(200,content=b'x'*(1048576+65537))
                if mode in ('rate','error'):return httpx.Response(429 if mode=='rate' else 500,json={'error':{'message':'private diagnostic'}})
                text='{' if mode=='malformed' else '{"ok":"yes"}' if mode in ('schema','validate_only_schema') else '{"ok":true}'
                if api=='chat_completions':
                    value={'id':'fixture','created':1,'object':'chat.completion','model':'fixture','choices':[{'index':0,'finish_reason':'length' if mode=='incomplete' else 'stop','message':{'role':'assistant','content':text,'refusal':'private refusal' if mode=='refusal' else None}}],'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}}
                    if mode.startswith('validate_only'):assert 'response_format' not in body
                    else:
                        assert body['response_format']['type']=='json_schema'
                        assert body['response_format']['json_schema']['name']==('qc_fixture' if mode=='options' else 'completion')
                else:
                    value={'id':'resp_fixture','created_at':1,'object':'response','model':'fixture','status':'incomplete' if mode=='incomplete' else 'completed','output':[{'type':'reasoning','id':'rs_fixture','summary':[]},{'type':'message','id':'msg_fixture','role':'assistant','status':'completed','content':[{'type':'refusal','refusal':'private refusal'}] if mode=='refusal' else [{'type':'output_text','text':text,'annotations':[]}]}],'usage':{'input_tokens':7,'output_tokens':3,'total_tokens':10}}
                    if mode=='item_incomplete':value['output'][1]['status']='incomplete'
                    if mode=='wrong_role':value['output'][1]['role']='user'
                    if mode=='split_text':
                        value['output'][1]['content']=[{'type':'output_text','text':'{"ok":','annotations':[]},{'type':'output_text','text':'tr','annotations':[]}]
                        value['output'].append({'type':'message','id':'msg_second','role':'assistant','status':'completed','content':[{'type':'output_text','text':'ue}','annotations':[]}]})
                    if mode.startswith('validate_only'):assert not body.get('text',{}).get('format')
                    else:
                        assert body['text']['format']['type']=='json_schema'
                        assert body['text']['format']['name']==('qc_fixture' if mode=='options' else 'completion')
                return httpx.Response(200,json=value,headers={'x-request-id':'fixture-request'})
            result=await complete(actual_profile,{} if mode=='missing' else {'KEY':'fixture'},finder,actual_request,expires=time.monotonic()+30,check=current,transport=httpx.MockTransport(receive))
            assert result['status']==expected,(api,mode,result)
            assert len(calls)==(0 if mode=='missing' else 1)
            if mode in ('success','validate_only','split_text'):assert result['data']=={'ok':True} and result['request_id']=='fixture-request' and result['usage']=={'input_tokens':7,'output_tokens':3}
            assert 'private' not in json.dumps(result)
            reports.append([api,mode,result['status']])
    print(json.dumps(reports))
asyncio.run(main())

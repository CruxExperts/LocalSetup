import subprocess,sys,json
from pathlib import Path


def test_direct_sdk_completion_both_interfaces():
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run([sys.executable,'-I','-B',str(root/'ls/tests/sdk_completion_fixture.py'),str(root)],capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    assert len(json.loads(result.stdout))==47


def test_compressed_completion_refused_before_read():
    import asyncio
    from ls.core.agent.completion_response import Capture,Rejected
    class Response:
        headers={'content-encoding':'gzip'}
        closed=False
        async def aiter_bytes(self):
            raise AssertionError('Compressed content read')
            yield b''
        async def aclose(self):self.closed=True
    response=Response()
    try:asyncio.run(Capture('responses',lambda:None)(response))
    except Rejected as error:assert error.status=='provider_error'
    else:raise AssertionError('Compressed response accepted')
    assert response.closed

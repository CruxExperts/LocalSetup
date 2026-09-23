import hashlib
import json
import time

import pytest

from ls.core.agent.session_branch import branch
from ls.core.agent.session_owner import lease
from ls.tests.test_session_owner import state, own, broker
from ls.tests.test_checkpoint_store import save


def call(state, broker, checkpoint, **kw):
    return branch(state, **(dict(source_task='task', source_session='session',
        checkpoint=checkpoint, task='fork-task', session='fork', workspace=broker.grant.root,
        profile='a'*64, expires=time.monotonic()+5) | kw))


def test_native_branch_exact_history_empty_operations_and_no_source_change(state, broker):
    with own(state, broker) as owner:
        digest=owner.save_checkpoint(b'[{"kind":"request","parts":[]}]',profile='a'*64,
                                    run_id='run',step=0,state='complete')
        root=owner.root
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result=call(state,broker,digest)
    assert result['source_checkpoint']==digest and result['mode']=='native'
    with lease(state,task='fork-task',session='fork',workspace=broker.grant.root,
               expires=time.monotonic()+5,create=False) as target:
        assert target.inspect()=={}
        assert target.resume_checkpoint(result['checkpoint'],profile='a'*64)==b'[{"kind":"request","parts":[]}]'
        assert json.loads((target.root/'branch.json').read_text())==result
        with pytest.raises(PermissionError,match='session task'):
            target.read_page(broker,'src/a.txt',for_provider=True)
    assert before=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(FileExistsError):call(state,broker,digest)


@pytest.mark.parametrize('mode',['incompatible','interrupted','uncertain','stale','same'])
def test_branch_refuses_before_destination_creation(state, broker, mode):
    with own(state,broker) as owner:
        digest=save(owner,state='interrupted' if mode=='interrupted' else 'complete')
        if mode=='uncertain':owner._journal.begin('process',{'argv_sha256':'a'*64,'snapshot_sha256':'b'*64})
        if mode=='stale':owner.write(broker,'src/a.txt',b'changed',expected_before=hashlib.sha256(b'original').hexdigest())
    kwargs={'profile':'b'*64} if mode=='incompatible' else {'session':'session'} if mode=='same' else {}
    with pytest.raises((ValueError,PermissionError)):call(state,broker,digest,**kwargs)
    assert not (state/hashlib.sha256(b'fork').hexdigest()).exists()


def test_new_lease_refuses_existing_empty_directory_and_invalid_modes(state,broker):
    target=state/hashlib.sha256(b'fork').hexdigest();target.mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        with lease(state,task='fork-task',session='fork',workspace=broker.grant.root,expires=time.monotonic()+5,new=True):pass
    assert list(target.iterdir())==[]
    with pytest.raises(ValueError):
        with lease(state,task='fork-task',session='fork',workspace=broker.grant.root,expires=time.monotonic()+5,new=True,create=False):pass


def test_portable_conversion_failure_or_revocation_creates_no_destination(state,broker,monkeypatch):
    from ls.core.agent import portable_history
    with own(state,broker) as owner:digest=save(owner)
    def failed(*args,**kwargs):raise RuntimeError('conversion failed')
    monkeypatch.setattr(portable_history,'convert',failed)
    with pytest.raises(RuntimeError):call(state,broker,digest,portable=True,runtimes=state.parent/'runtimes',profile='b'*64)
    assert not (state/hashlib.sha256(b'fork').hexdigest()).exists()
    def revoked(owner,*args,**kwargs):
        owner._closed.set()
        return b'[]'
    monkeypatch.setattr(portable_history,'convert',revoked)
    with pytest.raises(PermissionError):call(state,broker,digest,portable=True,runtimes=state.parent/'runtimes',profile='b'*64)
    assert not (state/hashlib.sha256(b'fork').hexdigest()).exists()

from dataclasses import replace
import base64
import json
import os
from pathlib import Path
import time
import pytest

from ls.core.agent.file_grants import FileGrant
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.runtime_lock import runtime_use


def _collect_page_bytes(broker, first, name='src/a.txt', *, for_provider=False):
    content = bytearray()
    pages = []
    page = first
    while True:
        pages.append(page)
        raw = page['content'].encode('utf-8') if page['encoding'] == 'utf-8' else base64.b64decode(page['content'])
        assert len(raw) == page['bytes']
        assert len(raw) <= 8 * 1024
        assert raw.count(b'\n') <= 200
        assert page['bytes'] or page['next_cursor'] is None
        assert len(json.dumps(page, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()) <= 16 * 1024
        content.extend(raw)
        if page['next_cursor'] is None:
            return bytes(content), pages
        page = broker.read_page('task', 'session', name, page['next_cursor'], for_provider=for_provider)


def test_read_page_preserves_long_utf8_lines_and_character_boundaries(broker):
    payload = ('a' * 8191 + '🧪' * 2048 + '\nend').encode('utf-8')
    (broker.grant.root / 'src/a.txt').write_bytes(payload)

    first = broker.read_page('task', 'session', 'src/a.txt')
    assert first['bytes'] == 8191
    assert first['content'] == 'a' * 8191
    assert first['next_cursor']
    restored, pages = _collect_page_bytes(broker, first)

    assert restored == payload
    assert all(page['encoding'] == 'utf-8' for page in pages)


def test_read_page_encodes_invalid_utf8_binary_losslessly(broker):
    payload = b'\xff\x00' * 5000
    (broker.grant.root / 'src/a.txt').write_bytes(payload)

    restored, pages = _collect_page_bytes(broker, broker.read_page('task', 'session', 'src/a.txt'))

    assert restored == payload
    assert len(pages) == 2
    assert all(page['encoding'] == 'base64' for page in pages)


def test_read_page_limits_lines_and_rejects_files_above_broker_limit(broker):
    payload = b'line\n' * 201
    (broker.grant.root / 'src/a.txt').write_bytes(payload)
    first = broker.read_page('task', 'session', 'src/a.txt')

    assert first['content'].count('\n') == 200
    restored, pages = _collect_page_bytes(broker, first)
    assert restored == payload
    assert len(pages) == 2

    (broker.grant.root / 'src/a.txt').write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='8 MiB'):
        broker.read_page('task', 'session', 'src/a.txt')


def test_read_page_rejects_stale_or_forged_cursors(broker):
    path = broker.grant.root / 'src/a.txt'
    path.write_bytes(b'a' * (8 * 1024 + 1))
    cursor = broker.read_page('task', 'session', 'src/a.txt')['next_cursor']
    assert cursor

    path.write_bytes(b'b' * (8 * 1024 + 1))
    with pytest.raises(PermissionError, match='stale'):
        broker.read_page('task', 'session', 'src/a.txt', cursor)

    forged = ('A' if cursor[0] != 'A' else 'B') + cursor[1:]
    with pytest.raises(PermissionError, match='cursor'):
        broker.read_page('task', 'session', 'src/a.txt', forged)
    with pytest.raises(PermissionError, match='cursor'):
        broker.read_page('task', 'session', 'src/a.txt', 'A' * 1025)


def test_read_page_cursor_binds_path_task_session_grant_and_disclosure(broker):
    (broker.grant.root / 'src/a.txt').write_bytes(b'a' * (8 * 1024 + 1))
    (broker.grant.root / 'src/other.txt').write_bytes(b'b' * (8 * 1024 + 1))
    allowed = FileBroker(replace(broker.grant, disclose=('src',)), broker.lease_root)
    cursor = allowed.read_page('task', 'session', 'src/a.txt', for_provider=True)['next_cursor']
    assert cursor

    for task, session, name in (
        ('other', 'session', 'src/a.txt'),
        ('task', 'other', 'src/a.txt'),
        ('task', 'session', 'src/other.txt'),
    ):
        with pytest.raises(PermissionError):
            allowed.read_page(task, session, name, cursor, for_provider=True)
    with pytest.raises(PermissionError, match='cursor'):
        allowed.read_page('task', 'session', 'src/a.txt', cursor, for_provider=False)

    allowed.grant = replace(allowed.grant, disclose=('.',))
    with pytest.raises(PermissionError, match='cursor'):
        allowed.read_page('task', 'session', 'src/a.txt', cursor, for_provider=True)


@pytest.fixture
def broker(tmp_path):
    root=tmp_path/'project';root.mkdir()
    lease=tmp_path/'leases';lease.mkdir(mode=0o700)
    (root/'src').mkdir();(root/'src/a.txt').write_bytes(b'original')
    grant=FileGrant('task','session',root,('src',),('src',),(),time.monotonic()+5)
    return FileBroker(grant,lease)


def test_authority_and_disclosure_are_separate(broker):
    assert broker.read('task','session','src/a.txt')==b'original'
    with pytest.raises(PermissionError,match='disclosure'):
        broker.read('task','session','src/a.txt',for_provider=True)
    allowed=FileBroker(replace(broker.grant,disclose=('src',)),broker.lease_root)
    assert allowed.read('task','session','src/a.txt',for_provider=True)==b'original'
    for task,session,name in [('other','session','src/a.txt'),('task','other','src/a.txt'),('task','session','outside')]:
        with pytest.raises(PermissionError):broker.read(task,session,name)


def test_atomic_write_preserves_mode_and_attributes(broker):
    path=broker.grant.root/'src/a.txt';path.chmod(0o750)
    os.setxattr(path,'user.fixture',b'preserve')
    inode=path.stat().st_ino
    broker.write('task','session','src/a.txt',b'replacement')
    assert path.read_bytes()==b'replacement' and path.stat().st_ino!=inode
    assert path.stat().st_mode&0o777==0o750
    assert os.getxattr(path,'user.fixture')==b'preserve'
    broker.write('task','session','src/new.txt',b'new')
    assert (path.parent/'new.txt').read_bytes()==b'new'
    assert not list(path.parent.glob('.lscli-write-*'))


@pytest.mark.parametrize('name',['../escape','/tmp/escape','src/../a','src//a','src/.git/config','src/.env','src/AGENTS.md'])
def test_unsafe_and_protected_writes_refused(broker,name):
    with pytest.raises(PermissionError):broker.write('task','session',name,b'bad')


def test_symlinks_and_hardlinks_refused(broker,tmp_path):
    root=broker.grant.root
    outside=tmp_path/'outside';outside.write_bytes(b'private')
    (root/'src/link').symlink_to(outside)
    (root/'src/dir').symlink_to(tmp_path,target_is_directory=True)
    os.link(outside,root/'src/hard')
    for name in ('src/link','src/dir/outside','src/hard'):
        with pytest.raises((OSError,PermissionError)):broker.read('task','session',name)
        with pytest.raises((OSError,PermissionError)):broker.write('task','session',name,b'bad')
    assert outside.read_bytes()==b'private'


def test_revocation_deadline_and_lease(broker):
    expired=FileBroker(replace(broker.grant,expires=time.monotonic()-1),broker.lease_root)
    with pytest.raises(PermissionError):expired.write('task','session','src/a.txt',b'bad')
    short=FileBroker(replace(broker.grant,expires=time.monotonic()+.03),broker.lease_root)
    with runtime_use(broker.lease_root):
        with pytest.raises(TimeoutError):short.write('task','session','src/a.txt',b'bad')
    broker.grant.revoked.set()
    with pytest.raises(PermissionError):broker.read('task','session','src/a.txt')


def test_revocation_during_write_preserves_target(broker,monkeypatch):
    original=os.fsync
    def revoke(fd):
        original(fd);broker.grant.revoked.set()
    monkeypatch.setattr(os,'fsync',revoke)
    with pytest.raises(PermissionError):broker.write('task','session','src/a.txt',b'bad')
    assert (broker.grant.root/'src/a.txt').read_bytes()==b'original'
    assert not list((broker.grant.root/'src').glob('.lscli-write-*'))


def test_changed_read_is_not_returned(broker,monkeypatch):
    original=os.read
    changed=[False]
    def mutate(fd,size):
        data=original(fd,size)
        if data and not changed[0]:
            changed[0]=True
            (broker.grant.root/'src/a.txt').write_bytes(b'changed')
        return data
    monkeypatch.setattr(os,'read',mutate)
    with pytest.raises(PermissionError,match='changed during read'):
        broker.read('task','session','src/a.txt')


def test_oversized_write_preserves_original(broker):
    with pytest.raises(ValueError,match='8 MiB'):
        broker.write('task','session','src/a.txt',b'x'*(8*1024*1024+1))
    assert (broker.grant.root/'src/a.txt').read_bytes()==b'original'


def test_protected_grant_root_and_mutable_scopes_refused(broker):
    with pytest.raises(ValueError,match='protected'):
        replace(broker.grant,root=broker.grant.root/'.git')
    with pytest.raises(ValueError,match='immutable'):
        replace(broker.grant,read=['src'])

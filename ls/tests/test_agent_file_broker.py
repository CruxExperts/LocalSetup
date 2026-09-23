from dataclasses import replace
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import pytest

from ls.core.agent.file_grants import FileGrant
from ls.core.agent.file_broker import FileBroker
from ls.core.agent.runtime_lock import runtime_use
from ls.core.openpgp import opening as opening_api
from ls.core.openpgp.contracts import EnvelopeHeader, EnvelopePolicy, LocalTrust


_OPENPGP_SIGNER = 'A' * 40
_OPENPGP_RECIPIENT = 'B' * 40


def _openpgp_authority(tmp_path, monkeypatch, payload, *, signature_valid=True):
    home = tmp_path / 'selected-decrypt-home'
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    header = EnvelopeHeader().to_json().encode('utf-8')
    envelope = struct.pack('>H', len(header)) + header + b'ciphertext'
    manifest = json.dumps(
        {
            'header': EnvelopeHeader().to_json(),
            'payload_length': len(payload),
            'payload_sha256': hashlib.sha256(payload).hexdigest(),
            'recipient_fingerprints': [_OPENPGP_RECIPIENT],
            'signer_fingerprint': _OPENPGP_SIGNER,
        },
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')
    inner = (
        b'localsetup.openpgp-inner\x00\x01'
        + struct.pack('>I', len(manifest))
        + manifest
        + payload
    )
    listing = (
        b':pubkey enc packet: version 3, algo 1, keyid 0000000000000000\n'
        b':encrypted data packet:\n'
    )
    signature = (
        b'[GNUPG:] BADSIG 0123456789ABCDEF invalid\n'
        if not signature_valid
        else (
            b'[GNUPG:] NEWSIG\n'
            b'[GNUPG:] GOODSIG 0123456789ABCDEF Test Signer\n'
            + f'[GNUPG:] VALIDSIG {_OPENPGP_SIGNER} 20260923 1790123456 0 4 0 1 8 00\n'.encode('ascii')
        )
    )
    diagnostics = (
        b'[GNUPG:] DECRYPTION_INFO 2 9 0 0\n'
        b'[GNUPG:] GOODMDC\n'
        b'[GNUPG:] DECRYPTION_OKAY\n'
        + signature
    )

    def fake_run_gpg(_executable, _selected_home, arguments, **_options):
        if '--list-packets' in arguments:
            return b'', listing
        return inner, diagnostics

    monkeypatch.setattr(opening_api.shutil, 'which', lambda name: '/usr/bin/gpg' if name in {'gpg', 'gpg2'} else None)
    monkeypatch.setattr(opening_api._envelope, '_run_gpg', fake_run_gpg)
    policy = EnvelopePolicy(
        expected_signers=(_OPENPGP_SIGNER,),
        expected_recipients=(_OPENPGP_RECIPIENT,),
    )
    return envelope, {
        'gnupg_home': home,
        'policy': policy,
        'local_trust': LocalTrust({_OPENPGP_SIGNER}),
    }


def _collect_page_bytes(broker, first, name='src/a.txt', *, for_provider=False, **read_options):
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
        page = broker.read_page(
            'task', 'session', name, page['next_cursor'],
            for_provider=for_provider, **read_options,
        )


def test_read_page_decrypts_verified_envelope_and_pages_binary_losslessly(
    broker, tmp_path, monkeypatch
):
    payload = b'\xff\x00' * 5000
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)

    first = broker.read_page('task', 'session', 'src/a.txt', **authority)
    restored, pages = _collect_page_bytes(broker, first, **authority)

    assert restored == payload
    assert len(pages) == 2
    assert all(page['encoding'] == 'base64' for page in pages)


def test_read_page_fails_closed_for_malformed_envelope_without_authority(broker):
    (broker.grant.root / 'src/a.txt').write_bytes(
        b'\x00\x01{"format":"localsetup.openpgp-envelope","schema_version":1}'
    )

    with pytest.raises(PermissionError, match='explicit decryption authority'):
        broker.read_page('task', 'session', 'src/a.txt')


@pytest.mark.parametrize('suffix', ['', '🗝' * 100])
def test_plaintext_mentioning_openpgp_remains_plaintext(broker, suffix):
    payload = 'Use localsetup.openpgp for verified documents.\n' + suffix
    (broker.grant.root / 'src/a.txt').write_text(payload)
    page = broker.read_page('task', 'session', 'src/a.txt')
    assert page['content'] == payload
    assert page['next_cursor'] is None



@pytest.mark.parametrize('malformation', ('bad-header-length', 'missing-ciphertext'))
def test_read_page_rejects_malformed_or_incomplete_envelopes(
    broker, tmp_path, monkeypatch, malformation
):
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, b'not released')
    if malformation == 'bad-header-length':
        envelope = b'\x00\x01' + envelope[2:]
    else:
        envelope = envelope[:-len(b'ciphertext')]
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)

    with pytest.raises(PermissionError) as raised:
        broker.read_page('task', 'session', 'src/a.txt', **authority)

    assert str(raised.value) == 'Protected OpenPGP envelope could not be verified'
    assert raised.value.__context__ is None

def test_read_page_sanitizes_signature_failure_and_never_returns_ciphertext(
    broker, tmp_path, monkeypatch
):
    envelope, authority = _openpgp_authority(
        tmp_path, monkeypatch, b'secret plaintext marker', signature_valid=False,
    )
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)

    with pytest.raises(PermissionError) as raised:
        broker.read_page('task', 'session', 'src/a.txt', **authority)

    assert str(raised.value) == 'Protected OpenPGP envelope could not be verified'
    assert raised.value.__context__ is None
    assert b'ciphertext' not in str(raised.value).encode()
    assert b'secret plaintext marker' not in str(raised.value).encode()


def test_read_page_encrypted_cursor_binds_policy_and_ciphertext(
    broker, tmp_path, monkeypatch
):
    payload = b'x' * (8 * 1024 + 1)
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    path = broker.grant.root / 'src/a.txt'
    path.write_bytes(envelope)
    cursor = broker.read_page('task', 'session', 'src/a.txt', **authority)['next_cursor']
    assert cursor

    changed_authorities = (
        {**authority, 'local_trust': LocalTrust({'D' * 40})},
        {
            **authority,
            'policy': EnvelopePolicy(
                expected_signers=(_OPENPGP_SIGNER,),
                expected_recipients=('C' * 40,),
            ),
        },
    )
    for changed_authority in changed_authorities:
        with pytest.raises(PermissionError, match='cursor'):
            broker.read_page('task', 'session', 'src/a.txt', cursor, **changed_authority)

    path.write_bytes(envelope[:-1] + bytes([envelope[-1] ^ 1]))
    with pytest.raises(PermissionError, match='stale'):
        broker.read_page('task', 'session', 'src/a.txt', cursor, **authority)



@pytest.mark.parametrize('header_change', ('mutated-format', 'escaped-format'))
def test_read_page_classifies_mutated_and_escaped_openpgp_headers(
    broker, tmp_path, monkeypatch, header_change
):
    payload = b'verified payload'
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    header_length = struct.unpack('>H', envelope[:2])[0]
    header = envelope[2:2 + header_length]
    marker = b'localsetup.openpgp-envelope'
    replacement = (
        b'localsetup.openpgp-envelopf'
        if header_change == 'mutated-format'
        else b'localsetup.openpgp\\u002denvelope'
    )
    changed_header = header.replace(marker, replacement)
    assert changed_header != header
    path = broker.grant.root / 'src/a.txt'
    path.write_bytes(struct.pack('>H', len(changed_header)) + changed_header + envelope[2 + header_length:])

    with pytest.raises(PermissionError, match='explicit decryption authority'):
        broker.read_page('task', 'session', 'src/a.txt')

    # The outer header bytes are authenticated, including JSON spelling.
    with pytest.raises(PermissionError) as raised:
        broker.read_page('task', 'session', 'src/a.txt', **authority)
    assert str(raised.value) == 'Protected OpenPGP envelope could not be verified'
    assert raised.value.__context__ is None


def test_read_page_reuses_verified_plaintext_and_consumes_encrypted_cursors(
    broker, tmp_path, monkeypatch
):
    payload = b'x' * (2 * 8 * 1024 + 1)
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)
    original = opening_api._envelope._run_gpg
    decryptions = []

    def count_decryptions(executable, home, arguments, **options):
        if '--decrypt' in arguments:
            decryptions.append(None)
        return original(executable, home, arguments, **options)

    monkeypatch.setattr(opening_api._envelope, '_run_gpg', count_decryptions)
    first = broker.read_page('task', 'session', 'src/a.txt', **authority)
    cursor = first['next_cursor']
    assert cursor
    second = broker.read_page('task', 'session', 'src/a.txt', cursor, **authority)
    assert second['next_cursor']
    assert len(decryptions) == 1

    with pytest.raises(PermissionError, match='cursor'):
        broker.read_page('task', 'session', 'src/a.txt', cursor, **authority)
    assert broker._verified_page_cache is None
    assert len(decryptions) == 1


def test_read_page_clear_cache_wipes_retained_plaintext_idempotently(
    broker, tmp_path, monkeypatch
):
    payload = b'private page data' * 1024
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)

    page = broker.read_page('task', 'session', 'src/a.txt', **authority)
    assert page['next_cursor']
    cache = broker._verified_page_cache
    assert cache is not None
    wiped = []

    class ObservedBuffer(bytearray):
        def clear(self):
            wiped.append(bytes(self))
            super().clear()

    cache.plaintext = ObservedBuffer(cache.plaintext)
    retained = cache.plaintext
    assert bytes(retained) == payload

    broker.clear_page_cache()
    assert broker._verified_page_cache is None
    assert wiped == [b'\x00' * len(payload)]
    assert not retained
    broker.clear_page_cache()
    assert wiped == [b'\x00' * len(payload)]

    with pytest.raises(PermissionError, match='cursor'):
        broker.read_page('task', 'session', 'src/a.txt', page['next_cursor'], **authority)


def test_read_page_limits_fresh_decrypt_attempts_per_window(
    broker, tmp_path, monkeypatch
):
    payload = b'x' * (8 * 1024 + 1)
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)
    original = opening_api._envelope._run_gpg
    decryptions = []

    def count_decryptions(executable, home, arguments, **options):
        if '--decrypt' in arguments:
            decryptions.append(None)
        return original(executable, home, arguments, **options)

    monkeypatch.setattr(opening_api._envelope, '_run_gpg', count_decryptions)
    for _ in range(2):
        assert broker.read_page('task', 'session', 'src/a.txt', **authority)['next_cursor']

    with pytest.raises(PermissionError, match='decryption budget'):
        broker.read_page('task', 'session', 'src/a.txt', **authority)
    assert len(decryptions) == 2


def test_read_page_rejects_protected_chain_over_page_work_cap(
    broker, tmp_path, monkeypatch
):
    payload = b'x' * (3 * 8 * 1024)
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, payload)
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)
    first = broker.read_page('task', 'session', 'src/a.txt', **authority)
    assert first['next_cursor']
    monkeypatch.setattr('ls.core.agent.file_broker._MAX_PROTECTED_PAGES', 2)

    with pytest.raises(PermissionError, match='2-page limit'):
        broker.read_page('task', 'session', 'src/a.txt', first['next_cursor'], **authority)
    assert broker._verified_page_cache is None


def test_read_page_rechecks_live_grant_after_envelope_verification(
    broker, tmp_path, monkeypatch
):
    envelope, authority = _openpgp_authority(tmp_path, monkeypatch, b'not disclosed')
    (broker.grant.root / 'src/a.txt').write_bytes(envelope)
    original = opening_api._envelope._run_gpg

    def revoke_after_decrypt(executable, home, arguments, **options):
        result = original(executable, home, arguments, **options)
        if '--decrypt' in arguments:
            broker.grant.revoked.set()
        return result

    monkeypatch.setattr(opening_api._envelope, '_run_gpg', revoke_after_decrypt)
    with pytest.raises(PermissionError, match='revoked') as raised:
        broker.read_page('task', 'session', 'src/a.txt', **authority)

    assert b'not disclosed' not in str(raised.value).encode()



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

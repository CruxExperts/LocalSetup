"""Bounded IMAP carrier reads used by the binary Agent Q envelope."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.mail_protocol_imap import ImapAdapter
from scripts.mail_protocol_support import MailControlError

RAW = b'Subject: opaque\r\nContent-Type: application/octet-stream\r\n\r\nexact\x00bytes'


class Client:
    def __init__(self, *, size=None, raw=RAW, returned_uid=b'7', extra=False):
        self.size = len(RAW) if size is None else size
        self.raw, self.returned_uid, self.extra = raw, returned_uid, extra
        self.calls = []

    def uid(self, command, uid, spec):
        self.calls.append((command, uid, spec))
        if spec == '(UID RFC822.SIZE)':
            return 'OK', [f'1 (UID 7 RFC822.SIZE {self.size})'.encode()]
        header = b'1 (UID ' + self.returned_uid + f' RFC822.SIZE {self.size} BODY[]<0> {{{len(self.raw)}}}'.encode()
        rows = [(header, self.raw), b')']
        if self.extra:
            rows.append((header, self.raw))
        return 'OK', rows


def test_bounded_fetch_requests_partial_and_preserves_binary_payload():
    client = Client()
    message = ImapAdapter()._fetch_message_object(client, '7', max_message_bytes=1024)
    assert message.get_payload(decode=True) == b'exact\x00bytes'
    assert client.calls == [('FETCH', '7', '(UID RFC822.SIZE)'),
                            ('FETCH', '7', '(UID RFC822.SIZE BODY.PEEK[]<0.1025>)')]


def test_oversized_message_is_rejected_before_body_fetch():
    client = Client(size=1025)
    with pytest.raises(MailControlError) as error:
        ImapAdapter()._fetch_message_object(client, '7', max_message_bytes=1024)
    assert error.value.code == 'MESSAGE_TOO_LARGE'
    assert len(client.calls) == 1


@pytest.mark.parametrize('kwargs', [{'raw': RAW[:-1]}, {'raw': RAW+b'extra'},
                                   {'returned_uid': b'8'}, {'extra': True}])
def test_incomplete_changed_or_ambiguous_response_is_never_parsed(kwargs, monkeypatch):
    import scripts.mail_protocol_imap as module
    monkeypatch.setattr(module.email, 'message_from_bytes', lambda raw: pytest.fail('unverified body parsed'))
    with pytest.raises(MailControlError) as error:
        ImapAdapter()._fetch_message_object(Client(**kwargs), '7', max_message_bytes=1024)
    assert error.value.code == 'IMAP_FETCH_FAILED'


@pytest.mark.parametrize('limit', [True, '1024', 0, 8*1024*1024+1])
def test_invalid_bound_rejected_before_fetch(limit):
    client = Client()
    with pytest.raises(MailControlError):
        ImapAdapter()._fetch_message_object(client, '7', max_message_bytes=limit)
    assert client.calls == []


def test_default_fetch_keeps_existing_protocol():
    client = Client()
    message = ImapAdapter()._fetch_message_object(client, '7')
    assert message.get_payload(decode=True) == b'exact\x00bytes'
    assert client.calls == [('FETCH', '7', '(BODY.PEEK[] FLAGS)')]


def test_uid_and_size_may_follow_the_imap_body_literal():
    class Reordered(Client):
        def uid(self, command, uid, spec):
            if spec == '(UID RFC822.SIZE)':
                return super().uid(command, uid, spec)
            return 'OK', [(f'1 (BODY[]<0> {{{len(RAW)}}}'.encode(), RAW),
                          f' UID 7 RFC822.SIZE {len(RAW)})'.encode()]
    message = ImapAdapter()._fetch_message_object(Reordered(), '7', max_message_bytes=1024)
    assert message.get_payload(decode=True) == b'exact\x00bytes'

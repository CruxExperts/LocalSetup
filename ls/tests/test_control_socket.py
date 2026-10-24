from pathlib import Path
import tempfile
import threading
import time

import pytest

from ls.core.agent.control_socket import Listener, connect


@pytest.fixture
def selected():
    with tempfile.TemporaryDirectory(prefix='lsctl-') as root:
        yield Path(root)/'socket'


def test_private_socket_accept_and_owned_cleanup(selected):
    listener = Listener(selected)
    try:
        assert selected.stat().st_mode & 0o777 == 0o600
        client = connect(selected, expires=time.monotonic()+2)
        server = listener.accept(expires=time.monotonic()+2, cancelled=threading.Event())
        client.close(); server.close()
    finally:
        listener.close()
    assert not selected.exists()
    listener.close()


def test_existing_or_replaced_path_is_never_deleted(selected):
    selected.write_text('retain')
    with pytest.raises(OSError): Listener(selected)
    assert selected.read_text() == 'retain'
    selected.unlink()
    listener = Listener(selected)
    selected.unlink(); selected.write_text('replacement')
    with pytest.raises(PermissionError): listener.close()
    assert selected.read_text() == 'replacement'


def test_accept_cancellation_and_shared_directory_rejected(selected):
    listener = Listener(selected)
    cancelled = threading.Event(); cancelled.set()
    try:
        with pytest.raises(TimeoutError):
            listener.accept(expires=time.monotonic()+10, cancelled=cancelled)
    finally: listener.close()
    selected.parent.chmod(0o755)
    try:
        with pytest.raises(ValueError): Listener(selected)
    finally: selected.parent.chmod(0o700)


def test_client_rejects_symlink_and_nonprivate_socket(selected):
    listener = Listener(selected)
    link = selected.with_name('link'); link.symlink_to(selected)
    try:
        with pytest.raises(PermissionError): connect(link, expires=time.monotonic()+2)
        selected.chmod(0o666)
        with pytest.raises(PermissionError): connect(selected, expires=time.monotonic()+2)
    finally:
        listener.close()


def test_relay_cli_failure_is_redacted_and_stdout_stays_protocol_only(selected, capsys):
    from ls.core.agent.cli import main
    assert main(['control-relay', '--socket', str(selected), '--timeout', 'nan']) == 2
    output = capsys.readouterr()
    assert not output.out and 'timeout' in output.err
    assert main(['control-relay', '--socket', str(selected), '--timeout', '1']) == 2
    output = capsys.readouterr()
    assert not output.out and str(selected) not in output.err and 'Traceback' not in output.err

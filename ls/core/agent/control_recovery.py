"""Load exact historical control identity under a newly authorized live owner."""
import os
import stat

from .broker_rpc import _decode
from .control_envelope import ControlBinding
from .operation_journal import DIGEST, IDENTIFIER, Journal
from .session_owner import _private


def target(owner, run_id, expected_digest):
    """Saved bytes identify evidence; only the caller's current grant authorizes use."""
    owner._check()
    if (type(run_id) is not str or not IDENTIFIER.fullmatch(run_id) or run_id in {'.', '..'}
            or type(expected_digest) is not str or not DIGEST.fullmatch(expected_digest)):
        raise ValueError('Recovery requires the original run and trusted binding digest')
    selected = owner.root/'controls'/run_id
    directory = _private(selected)
    fd = None
    try:
        fd = os.open('binding.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o7077:
            raise PermissionError('Historical control binding must be a private owned file')
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ValueError('Historical control binding exceeds limit')
        value = _decode(raw)
        if type(value) is not dict or type(value.get('scope')) is not list:
            raise ValueError('Invalid historical control binding')
        try:
            binding = ControlBinding(**(value | {'scope': tuple(value['scope'])}))
        except TypeError:
            raise ValueError('Invalid historical control binding') from None
        if (binding.digest() != expected_digest or binding.run != run_id
                or (binding.task, binding.session) != (owner._journal.task, owner._journal.session)):
            raise PermissionError('Historical control identity differs from the selected evidence')
        journal_root = selected/'journal'
        os.close(_private(journal_root))
        owner._check()
        return binding, Journal(journal_root, task=binding.task, session=binding.session)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)

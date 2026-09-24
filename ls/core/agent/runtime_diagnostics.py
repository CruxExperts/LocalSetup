"""Static installed-state checks without initializing runtime or provider state."""
from pathlib import Path

from .profile_inventory import validate
from .profiles import document
from .runtime_install import selected


def runtime(root: Path) -> dict:
    try:
        # lstat distinguishes absent roots from dangling links; lease validation
        # rejects links, unsafe ownership and unsupported platforms.
        root.lstat()
    except FileNotFoundError:
        return {'status': 'missing'}
    except OSError:
        return {'status': 'invalid'}
    try:
        with selected(root.absolute(), timeout=1, create=False) as release:
            from .installed_capabilities import capabilities, dependencies, native
            return {'status': 'verified', 'dependencies': dependencies(release),
                    'native_sandbox': native(release), 'capabilities': capabilities(release)}
    except TimeoutError:
        return {'status': 'busy'}
    except FileNotFoundError:
        return {'status': 'incomplete'}
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        return {'status': 'invalid'}


def profiles(path: Path) -> dict:
    try:
        path.lstat()
    except FileNotFoundError:
        return {'status': 'missing', 'count': 0}
    except OSError:
        return {'status': 'invalid', 'count': 0}
    try:
        count = len(validate(document(path, trusted=True))['profiles'])
        return {'status': 'verified' if count else 'empty', 'count': count}
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        return {'status': 'invalid', 'count': 0}

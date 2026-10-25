import json
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-name contract")
def test_native_process_name_tracks_version_without_changing_arguments():
    code = """import json,sys
from pathlib import Path
from ls.core import process_identity
process_identity.framework_version = lambda: '9.8.7'
before = Path('/proc/self/cmdline').read_bytes()
assert process_identity.set_process_name()
print(json.dumps([Path('/proc/self/comm').read_text().strip(), before == Path('/proc/self/cmdline').read_bytes()]))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["LS-v9.8.7", True]

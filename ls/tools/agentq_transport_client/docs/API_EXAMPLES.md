# Agent Q transport client – API examples

## Python: read framework version

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("ls/tools/agentq_transport_client")))
sys.path.insert(0, "ls")
from agentq_transport_client.version_util import read_framework_version, read_framework_hash

print(read_framework_version())
print(read_framework_hash())
```

## Python: stamp PRD

```python
from pathlib import Path
# After sys.path setup as above
from agentq_transport_client.prd_stamp import ensure_prd_stamp
ensure_prd_stamp(Path(".agent/queue/my.prd.md"), add_hash=True)
```

## CLI equivalents

```bash
python3 ls/tools/agentq_transport_client/agentq_cli.py version
python3 ls/tools/agentq_transport_client/agentq_cli.py stamp-prd .agent/queue/my.prd.md --hash
```

## Python: authenticated Agent Q envelope

The selected private registry must already refer to current authority stores.
The sender's GnuPG home must contain the peer's pinned public certificate.
The receiver chooses the expected peer from trusted configuration before
opening. The payload and its identities remain inside the binary envelope.

```python
from pathlib import Path
from agentq_transport_client.registry import load_registry_yaml
from agentq_transport_client.crypto_pipeline import seal_manifest, open_manifest

sender = load_registry_yaml(Path("/private/agent-a/registry.yaml"))
manifest = {
    "manifest_version": "1",
    "from_agent_id": "agent-a",
    "to_agent_ids": ["agent-b"],
    "prd_body": "# Authorized request\n",
    "prd_filename": "request.prd.md",
}
blob = seal_manifest(manifest, sender)

# Run on the recipient side, with its own private registry and keyring:
recipient = load_registry_yaml(Path("/private/agent-b/registry.yaml"))
verified_manifest, authority = open_manifest(blob, recipient, peer_id="agent-a")
assert verified_manifest["from_agent_id"] == "agent-a"
```

The normal consumer uses `ingest_blob_bytes()` or the CLI to record the exact
accepted ciphertext and promote it. `open_manifest()` alone does not create a
receipt. For an archive, `open_historical_manifest()` requires an exact
previously accepted ciphertext receipt and never promotes a queue item.

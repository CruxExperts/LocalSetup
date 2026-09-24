# Purpose: Agent Q transport client package (inbound/outbound adapters, PRD stamp).
# Created: 2026-03-09
# Last updated: 2026-03-09

import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from agentq_transport_client.version_util import (
    read_framework_hash,
    read_framework_version,
)

__all__ = ["read_framework_version", "read_framework_hash"]

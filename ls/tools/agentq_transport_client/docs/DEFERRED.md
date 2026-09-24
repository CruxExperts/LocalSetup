# Agent Q transport history and deferred adapters

The earlier PGPy, optional strict-GPG, `ship-mail-strict`, armored mail-body,
and encrypt-only paths were replaced by the current authenticated binary
envelope. They are historical compatibility inputs for manual migration, not
fallbacks in ordinary ingest. The
[client guide](USER_GUIDE.md) and
[protocol](../../../docs/AGENTIC_AGENT_TO_AGENT_PROTOCOL.md) describe current
commands and carriers.

Google Drive and Dropbox API adapters and a Telegram API adapter remain outside
the file-drop and mail implementation. A sync folder can still carry an opaque
file drop when both sides map it to their allowed roots. The
[historical build specification](../../../docs/AGENTIC_AGENT_Q_BIDIRECTIONAL_BUILD_SPEC.md)
preserves the original design sequence and rationale; its old command examples
must not be used as current CLI instructions.

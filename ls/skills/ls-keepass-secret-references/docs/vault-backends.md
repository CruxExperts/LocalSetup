# Vault Backends

## KeePassXC

KeePassXC is the preserved default capability guard, not an operational backend. The wrapper checks for the local binary and may run --version for diagnostics. It reports missing_backend when the binary is unavailable; otherwise every vault read or write returns interactive_backend_required before vault access. It never opens or unlocks a vault.

The safe write-field constants remain validation metadata for future approved backend work. They do not authorize writes in this package.

## Fake

The fake backend is for tests and examples. It can persist placeholder values to a JSON file with `--fake-store`, but that file is not a real vault.

## PyKeePass

PyKeePass is a possible future backend. It is not a dependency in this implementation.

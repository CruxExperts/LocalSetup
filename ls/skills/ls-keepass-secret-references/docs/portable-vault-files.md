# Portable Vault Files

KeePass `.kdbx` files are encrypted containers, but they are still sensitive operational assets. Do not commit them by default.

Keep keyfiles outside the repo. Store backup copies in an operator-approved encrypted location. The `vault-backup` command is dry-run by default and requires `--apply` to copy a configured database path.

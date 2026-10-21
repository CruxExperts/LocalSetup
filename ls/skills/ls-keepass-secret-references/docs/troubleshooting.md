# Troubleshooting

missing_backend: the optional KeePassXC capability diagnostic cannot find keepassxc-cli on PATH. Install it only when that diagnostic is required; map validation and reference parsing do not require it.

interactive_backend_required: KeePassXC vault operations are deliberately unavailable. Use mapping/config/reference validation or an approved human-operated secret-manager process.

invalid_secret_id: use lowercase letters, digits, dots, underscores, and hyphens only.

secret_value_in_file: remove passwords, tokens, keys, or passphrases from config or map files.

unknown_secret_id: add the canonical ID to the configured map or fix the alias.

unsupported_field: standard-field validation applies only to the fake fixture and a future approved backend. The current KeePassXC capability guard refuses every write.

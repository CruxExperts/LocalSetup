# KeePassXC Installation and Versioning

Install KeePassXC from the official project packages or your operating-system package manager. Verify signatures for downloaded binaries when practical.

Check local capability without opening a vault:

```bash
python3 scripts/verify_keepassxc.py --format json
```

The helper reports the local binary path, version text, and detected command help capabilities. It does not install, upgrade, or open KeePassXC.

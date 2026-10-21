#!/usr/bin/env bash
set -euo pipefail

python3 scripts/localsetup_secrets.py export-env \
  DATABASE_PASSWORD=postgres.box03.app1:password \
  --backend fake \
  --map examples/map.yaml

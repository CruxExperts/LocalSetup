# Scout Modes

## Static Mode

Static mode is default. It derives a scout report from repository metadata without calling a model or external command.

## Command Mode

Command mode runs only when explicitly configured:

```bash
STARREDREPOS_SCOUT_MODE=command \
STARREDREPOS_SCOUT_COMMAND='some-command --json' \
node scripts/scout-repo-metadata.mjs --input repo.json
```

The script passes JSON on stdin, enforces a timeout, parses JSON on stdout, validates the scout report shape, and marks unsupported claims as unverified.

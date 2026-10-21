# API and CLI References

Use `gh api` for authenticated GitHub API calls and set the REST API version header explicitly:

```bash
gh api -H 'X-GitHub-Api-Version: 2026-03-10' /user/starred
```

Compatibility note: local `gh 2.45.0` does not support `gh api --slurp`, so scripts handle pagination themselves.

Prefer REST for star inventory because the starring endpoint can return `starred_at`. Use GraphQL as a cross-check for viewer and counts.

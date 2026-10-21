# Node Runtime

Target the supported Node.js 22.x LTS line.

The scripts rely on:

- ESM modules
- built-in `fetch`
- `node:fs/promises`
- `node:child_process`
- `AbortController`

No npm install is required. This is a maintained exception for this skill because the requested bootstrap explicitly calls for Node ESM helpers.

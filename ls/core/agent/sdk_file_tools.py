"""Isolated SDK file tools delegate authority and effects to the supervisor."""
from __future__ import annotations

import sys

from .sdk_tool_checkpoint import pretool_checkpoint


def file_tools(finder, channel):
    if not sys.flags.isolated or not sys.dont_write_bytecode or sys.meta_path[0] is not finder:
        raise RuntimeError('SDK file tools require the active isolated worker importer')
    finder.verify_origins()
    from pydantic_ai.tools import Tool

    async def read_file(path: str, cursor: str | None = None) -> dict:
        """Read one verified bounded page. Pass next_cursor to continue without changing authority."""
        return await channel.request_async('file.read', {'path': path, 'cursor': cursor})

    async def refresh_context(directory: str) -> dict:
        """Refresh root-to-directory AGENTS.md instructions under explicit read/disclosure grants."""
        return await channel.request_async('context.refresh', {'directory':directory})

    async def list_files(path: str) -> dict:
        """List granted direct child files/directories; use a dot for the granted workspace root."""
        return await channel.request_async('file.list', {'path':path})

    async def search_files(paths: list[str], text: str) -> dict:
        """Find literal case-sensitive text in explicit granted UTF-8 files; results are bounded."""
        return await channel.request_async('file.search', {'paths':paths,'text':text})

    async def write_file(ctx, path: str, content: str, expected_before: str | None) -> dict:
        """Replace granted UTF-8 text only when its digest matches; null requires absence."""
        checkpoint = await pretool_checkpoint(finder,channel,ctx)
        return await channel.request_async('file.write', {'path':path,'content':content,
            'expected_before':expected_before,'checkpoint':checkpoint,'call_id':ctx.tool_call_id})

    # Explicit takes_ctx avoids resolving a local-scope annotation through globals.
    tools = (Tool(read_file, sequential=True, max_retries=0),
             Tool(write_file, takes_ctx=True, sequential=True, max_retries=0),
             Tool(search_files, sequential=True, max_retries=0),
             Tool(list_files, sequential=True, max_retries=0),
             Tool(refresh_context, sequential=True, max_retries=0))
    finder.verify_origins()
    return tools

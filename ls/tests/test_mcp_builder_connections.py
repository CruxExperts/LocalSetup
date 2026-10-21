from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "skills" / "ls-mcp-server-builder"
CONNECTIONS_PATH = PACKAGE_ROOT / "scripts" / "connections.py"
NODE_GUIDE = PACKAGE_ROOT / "references" / "node_mcp_server.md"


def _load_connections(monkeypatch):
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, headers):
            self.headers = headers
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            self.closed = True

    @asynccontextmanager
    async def streamable_http_client(*, url, http_client):
        captured.update(url=url, http_client=http_client)
        yield (object(), object(), lambda: None)

    modules = {
        "httpx2": SimpleNamespace(AsyncClient=FakeAsyncClient),
        "mcp": SimpleNamespace(ClientSession=object, StdioServerParameters=object),
        "mcp.client": ModuleType("mcp.client"),
        "mcp.client.sse": SimpleNamespace(sse_client=lambda **_kwargs: None),
        "mcp.client.stdio": SimpleNamespace(stdio_client=lambda *_args: None),
        "mcp.client.streamable_http": SimpleNamespace(
            streamable_http_client=streamable_http_client
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("mcp_builder_connections", CONNECTIONS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, captured


def test_streamable_http_uses_managed_httpx2_client(monkeypatch) -> None:
    module, captured = _load_connections(monkeypatch)

    async def exercise() -> None:
        connection = module.create_connection(
            "streamable-http",
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer test"},
        )
        connection._stack = module.AsyncExitStack()
        await connection._stack.__aenter__()
        transport = await connection._create_context()
        async with transport:
            client = captured["http_client"]
            assert captured["url"] == "https://example.test/mcp"
            assert client.headers == {"Authorization": "Bearer test"}
            assert client.closed is False
        await connection._stack.aclose()
        assert client.closed is True

    asyncio.run(exercise())


def test_removed_http_alias_is_rejected(monkeypatch) -> None:
    module, _ = _load_connections(monkeypatch)

    try:
        module.create_connection("http", url="https://example.test/mcp")
    except ValueError as exc:
        assert "streamable-http" in str(exc)
    else:
        raise AssertionError("legacy http alias must be rejected")


def test_node_v2_examples_have_current_executable_contracts() -> None:
    guide = NODE_GUIDE.read_text(encoding="utf-8")
    package_match = re.search(
        r"### package.json\n\n```json\n(.*?)\n```", guide, re.DOTALL
    )
    assert package_match is not None
    package = json.loads(package_match.group(1))

    assert package["engines"]["node"] == ">=20"
    assert package["dependencies"]["@modelcontextprotocol/server"].startswith("^2.")
    assert package["dependencies"]["zod"].startswith("^4.")

    complete_match = re.search(
        r"## Complete Example\n\n```typescript\n(.*?)\n```", guide, re.DOTALL
    )
    assert complete_match is not None
    example = complete_match.group(1)
    assert 'from "@modelcontextprotocol/server"' in example
    assert 'from "@modelcontextprotocol/server/stdio"' in example
    assert "void serveStdio(() => server);" in example
    assert "StdioServerTransport" not in example
    assert re.search(r"server\.registerTool\(.*?\n\);", example, re.DOTALL)


def test_guides_contain_no_removed_mcp_v1_apis() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py"}
    )
    for stale in (
        "FastMCP",
        "mcp.server.fastmcp",
        "@modelcontextprotocol/sdk",
        "SSEServerTransport",
        "mcp>=1.",
        "-t http",
    ):
        assert stale not in text

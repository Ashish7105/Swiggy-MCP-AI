"""Thin wrapper around the official MCP Python SDK for talking to a Swiggy
MCP server (Food, by default) as an authenticated end user.

This module intentionally does NOT know anything about restaurants, menus,
or carts. It knows how to:

  1. Authenticate via OAuth (browser consent + localhost callback), caching
     tokens on disk so you don't re-auth every run.
  2. Open an MCP ClientSession over Streamable HTTP.
  3. Discover whatever tools the server currently exposes.
  4. Call a tool by name with arguments and return the result.

Everything domain-specific (which tool to call, how to phrase the answer)
lives in orchestrator.py.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession, Tool
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from . import config


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + dynamically-registered client info to disk.

    One JSON file per MCP server, so re-running the CLI doesn't trigger a
    fresh browser consent every time, and refresh tokens get used instead.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


class _LocalCallbackServer:
    """Minimal localhost HTTP server that captures the single OAuth
    redirect (``?code=...&state=...``) and nothing else.

    This mirrors what Claude Desktop / Cursor do when they connect to the
    same Swiggy MCP server as a user-authenticated MCP client.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._result: AuthorizationCodeResult | None = None
        self._error: str | None = None
        self._event = asyncio.Event()

    async def wait_for_callback(self) -> AuthorizationCodeResult:
        import urllib.parse
        from http.server import BaseHTTPRequestHandler, HTTPServer

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib naming)
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                error = params.get("error", [None])[0]

                if error:
                    outer._error = error
                elif code:
                    outer._result = AuthorizationCodeResult(code=code, state=state)

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                message = (
                    "Authentication complete — you can close this tab and "
                    "return to the terminal."
                    if code
                    else f"Authentication failed: {error or 'no code received'}"
                )
                self.wfile.write(f"<html><body>{message}</body></html>".encode())

            def log_message(self, *_args):  # silence stdlib's default logging
                pass

        server = HTTPServer((self._host, self._port), Handler)
        loop = asyncio.get_event_loop()
        # Serve exactly one request, off the event loop, without blocking it.
        await loop.run_in_executor(None, server.handle_request)
        server.server_close()

        if self._result is not None:
            return self._result
        raise RuntimeError(f"OAuth callback failed: {self._error or 'no result'}")


def _build_oauth_provider(server_url: str, settings: config.Settings) -> OAuthClientProvider:
    token_path = config.TOKEN_DIR / f"{server_url.rstrip('/').rsplit('/', 1)[-1]}.json"
    storage = FileTokenStorage(token_path)

    async def redirect_handler(authorization_url: str) -> None:
        print(f"\nOpen this URL to log into Swiggy and approve access:\n  {authorization_url}\n")
        webbrowser.open(authorization_url)

    async def callback_handler() -> AuthorizationCodeResult:
        server = _LocalCallbackServer(settings.oauth_redirect_host, settings.oauth_redirect_port)
        return await server.wait_for_callback()

    client_metadata = OAuthClientMetadata(
        client_name="Swiggy Conversational Food Assistant (personal)",
        redirect_uris=[settings.oauth_redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        application_type="native",
    )

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


@dataclass
class MCPToolSource:
    """A connected, authenticated MCP session plus its discovered tools.

    Each Swiggy MCP server (Food / Instamart / Dineout) gets one of these.
    The orchestrator merges tools across whichever sources are connected.
    """

    name: str
    session: ClientSession
    tools: list[Tool]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        result = await self.session.call_tool(tool_name, arguments)
        return result


class SwiggyMCPConnection:
    """Connects to one Swiggy MCP server for the lifetime of the process.

    Usage:
        async with SwiggyMCPConnection(url, settings) as source:
            ... use source.tools / source.call_tool(...) ...
    """

    def __init__(self, server_url: str, settings: config.Settings, label: str = "food") -> None:
        self._server_url = server_url
        self._settings = settings
        self._label = label
        self._stack = AsyncExitStack()
        self.source: MCPToolSource | None = None

    async def __aenter__(self) -> MCPToolSource:
        oauth = _build_oauth_provider(self._server_url, self._settings)
        http_client = httpx2.AsyncClient(auth=oauth, timeout=30.0)
        await self._stack.enter_async_context(http_client)

        read_stream, write_stream = await self._stack.enter_async_context(
            streamable_http_client(self._server_url, http_client=http_client)
        )
        session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream, client_info=None)
        )
        await session.initialize()

        tools_result = await session.list_tools()
        self.source = MCPToolSource(name=self._label, session=session, tools=tools_result.tools)
        return self.source

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()

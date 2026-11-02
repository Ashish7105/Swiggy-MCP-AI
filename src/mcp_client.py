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
import html
import json
import urllib.parse
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


# Where to send the browser after a successful login, if anywhere. The web
# UI sets this to its own chat page; the CLI leaves it unset, so the page
# just says to go back to the terminal.
AFTER_LOGIN_URL: str | None = None


def _callback_page(succeeded: bool, error: str | None) -> str:
    """The page the browser lands on after Swiggy's login redirect."""
    if not succeeded:
        body = f"<h2>Login failed</h2><p>{html.escape(error or 'No authorization code received')}.</p>"
        refresh = ""
    elif AFTER_LOGIN_URL:
        url = html.escape(AFTER_LOGIN_URL, quote=True)
        body = f'<h2>Logged in to Swiggy</h2><p>Taking you back to the chat… <a href="{url}">Go now</a></p>'
        # replace(), not a link click, so Back doesn't return to this page
        # (its one-time code is already used).
        refresh = f"<script>setTimeout(() => location.replace({json.dumps(AFTER_LOGIN_URL)}), 800)</script>"
    else:
        body = "<h2>Logged in to Swiggy</h2><p>You can close this tab and return to the terminal.</p>"
        refresh = ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Swiggy login</title>"
        "<style>body{font:16px system-ui,sans-serif;text-align:center;padding:60px 16px}"
        "@media(prefers-color-scheme:dark){body{background:#141416;color:#ececee}a{color:#ff8a2b}}</style>"
        f"</head><body>{body}{refresh}</body></html>"
    )


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
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_callback_page(code is not None, error).encode())

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


# One Swiggy login covers every service: Swiggy issues the token for the
# whole host (resource=https://mcp.swiggy.com), and its docs say the
# servers share one session ("one OAuth, two MCP URLs"). So all services
# on a host share one provider — one login, one token file, one refresh.
# The provider object itself is shared, not just the file: a refresh may
# rotate the refresh token, and a second provider holding the old one in
# memory would then fail and force another browser login.
_providers: dict[str, OAuthClientProvider] = {}


def _build_oauth_provider(server_url: str, settings: config.Settings) -> OAuthClientProvider:
    parsed = urllib.parse.urlparse(server_url)
    host_url = f"{parsed.scheme}://{parsed.netloc}"
    if host_url not in _providers:
        _providers[host_url] = _new_oauth_provider(host_url, parsed.netloc, settings)
    return _providers[host_url]


def _new_oauth_provider(host_url: str, host: str, settings: config.Settings) -> OAuthClientProvider:
    storage = FileTokenStorage(config.TOKEN_DIR / f"{host}.json")

    async def redirect_handler(authorization_url: str) -> None:
        print(f"\nOpen this URL to log into Swiggy and approve access:\n  {authorization_url}\n")
        webbrowser.open(authorization_url)

    async def callback_handler() -> AuthorizationCodeResult:
        server = _LocalCallbackServer(settings.oauth_redirect_host, settings.oauth_redirect_port)
        return await server.wait_for_callback()

    client_metadata = OAuthClientMetadata(
        client_name="Swiggy Conversational Assistant (personal)",
        redirect_uris=[settings.oauth_redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        application_type="native",
    )

    return OAuthClientProvider(
        server_url=host_url,
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
        try:
            return await self._connect()
        except BaseException:
            # A failed login/connect must not leave half-open streams behind.
            await self._stack.aclose()
            raise

    async def _connect(self) -> MCPToolSource:
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

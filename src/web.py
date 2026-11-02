"""Local web chat UI: `python -m src.web`, then open http://127.0.0.1:8000.

Same conversations as the CLI (one per Swiggy service, each with its own
MCP connection and history), shown in a browser page. The page talks to
this server over one WebSocket: it picks a service, sends messages, and
answers confirmation prompts for destructive tools (placing an order,
booking a table) in a dialog instead of the terminal's y/N.

Personal use only, like the CLI: the server binds to 127.0.0.1 and only
accepts WebSocket connections from its own page. Without that origin
check, any website open in your browser could connect to it and place
orders with your Swiggy login.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import webbrowser
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config, mcp_client
from .services import OpenedService, open_service

HOST = "127.0.0.1"
PORT = int(os.environ.get("WEB_PORT", "8000"))
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
PAGE = Path(__file__).parent / "web" / "index.html"


class UIBridge:
    """Routes the orchestrators' confirmation prompts and status notes to
    whichever browser tab is connected. With no tab connected, anything
    needing confirmation is declined."""

    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self._pending: dict[int, asyncio.Future[bool]] = {}
        self._ids = itertools.count(1)

    async def send(self, message: dict[str, Any]) -> None:
        if self.ws is not None:
            try:
                await self.ws.send_json(message)
            except Exception:  # noqa: BLE001 - tab went away mid-send
                pass

    def notify(self, text: str) -> None:
        asyncio.get_running_loop().create_task(self.send({"type": "status", "text": text}))

    async def confirm(self, tool_name: str, arguments: dict[str, Any], summary: str) -> bool:
        if self.ws is None:
            return False
        confirm_id = next(self._ids)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[confirm_id] = future
        await self.send(
            {"type": "confirm", "id": confirm_id, "tool": tool_name, "args": arguments, "summary": summary}
        )
        try:
            return await future
        finally:
            self._pending.pop(confirm_id, None)

    def answer(self, confirm_id: int, approved: bool) -> None:
        future = self._pending.get(confirm_id)
        if future is not None and not future.done():
            future.set_result(approved)

    def detach(self, ws: WebSocket) -> None:
        if self.ws is ws:
            self.ws = None
            for future in self._pending.values():
                if not future.done():
                    future.set_result(False)


class ServiceManager:
    """Opens Swiggy services on request and keeps them open. Everything
    happens in one long-lived task because the MCP client's streams must be
    opened and closed in the same task."""

    def __init__(self, settings: config.Settings, bridge: UIBridge) -> None:
        self.settings = settings
        self.bridge = bridge
        self.opened: dict[str, OpenedService] = {}
        self._requests: asyncio.Queue[tuple[str, asyncio.Future[OpenedService]] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        async with AsyncExitStack() as stack:
            while (request := await self._requests.get()) is not None:
                name, future = request
                if name not in self.opened:
                    try:
                        self.opened[name] = await open_service(
                            name, self.settings, stack, confirm=self.bridge.confirm, notify=self.bridge.notify
                        )
                    except Exception as exc:  # noqa: BLE001 - reported to the page
                        future.set_exception(exc)
                        continue
                future.set_result(self.opened[name])

    async def get(self, name: str) -> OpenedService:
        if name in self.opened:
            return self.opened[name]
        future: asyncio.Future[OpenedService] = asyncio.get_running_loop().create_future()
        await self._requests.put((name, future))
        return await future

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self._requests.put(None)
        if self._task is not None:
            await self._task


settings = config.load_settings()
bridge = UIBridge()
manager = ServiceManager(settings, bridge)


def _service_info(name: str, opened: OpenedService) -> dict[str, Any]:
    return {"type": "service_ready", "service": name, "tools": len(opened.tool_names), "address": opened.address_note}


async def _open(name: str) -> None:
    if name not in manager.opened:
        await bridge.send(
            {
                "type": "status",
                "service": name,
                "text": f"Connecting to {name}… The first time, a Swiggy login tab opens in your browser.",
            }
        )
    try:
        opened = await manager.get(name)
    except Exception as exc:  # noqa: BLE001 - shown in the page
        await bridge.send({"type": "error", "service": name, "text": f"Couldn't connect to {name}: {exc}"})
        return
    await bridge.send(_service_info(name, opened))


async def _chat(name: str, text: str) -> None:
    try:
        opened = await manager.get(name)
        reply = await opened.orchestrator.send(text)
    except Exception as exc:  # noqa: BLE001 - keep the conversation alive
        await bridge.send({"type": "error", "service": name, "text": str(exc)})
        return
    await bridge.send({"type": "reply", "service": name, "text": reply})


async def index(_request) -> FileResponse:
    return FileResponse(PAGE)


async def chat_socket(ws: WebSocket) -> None:
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    if bridge.ws is not None:
        # One user, one tab: the newest tab takes over.
        await bridge.send({"type": "error", "text": "Opened in another tab; this one is disconnected."})
        bridge.detach(bridge.ws)
    bridge.ws = ws
    await ws.send_json(
        {
            "type": "hello",
            "services": list(settings.swiggy_services),
            "opened": [_service_info(n, o) for n, o in manager.opened.items()],
        }
    )

    busy: asyncio.Task[None] | None = None
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "confirm_reply":
                bridge.answer(int(message.get("id", 0)), bool(message.get("ok")))
                continue
            name = message.get("service")
            if name not in settings.swiggy_services:
                await ws.send_json({"type": "error", "text": f"Unknown service: {name}"})
                continue
            if busy is not None and not busy.done():
                await ws.send_json({"type": "error", "service": name, "text": "Still working on the last request."})
                continue
            if kind == "open":
                busy = asyncio.create_task(_open(name))
            elif kind == "message" and str(message.get("text", "")).strip():
                busy = asyncio.create_task(_chat(name, str(message["text"]).strip()))
    except WebSocketDisconnect:
        pass
    finally:
        bridge.detach(ws)


@asynccontextmanager
async def lifespan(_app: Starlette):
    manager.start()
    url = f"http://{HOST}:{PORT}"
    # After a Swiggy login, send the browser back to the chat, not to a
    # "return to the terminal" page.
    mcp_client.AFTER_LOGIN_URL = url
    print(f"Swiggy assistant running at {url} (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
    yield
    await manager.stop()


app = Starlette(
    routes=[Route("/", index), WebSocketRoute("/ws", chat_socket)],
    lifespan=lifespan,
)


def main() -> None:
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

"""REPL entrypoint: `python -m src.cli`.

Connects to Swiggy's Food MCP server as you (OAuth, browser consent on
first run), then hands the conversation to the orchestrator turn by turn.
"""

from __future__ import annotations

import asyncio
import sys

from . import config
from .mcp_client import SwiggyMCPConnection
from .orchestrator import Orchestrator, find_saved_address_id

BANNER = """\
Swiggy Conversational Food Assistant (personal use)
-----------------------------------------------------
Connecting to {url} ...
Type your request in plain English. Type 'exit' or Ctrl-D to quit.
"""


async def _confirm(tool_name: str, arguments: dict) -> bool:
    """CLI confirmation gate for order/payment tool calls."""
    print(f"\n[confirmation needed] About to run '{tool_name}' with:")
    print(f"  {arguments}")
    answer = await asyncio.get_event_loop().run_in_executor(
        None, input, "Proceed? [y/N]: "
    )
    return answer.strip().lower() in ("y", "yes")


async def _run() -> None:
    settings = config.load_settings()
    print(BANNER.format(url=settings.swiggy_food_url))

    async with SwiggyMCPConnection(settings.swiggy_food_url, settings) as source:
        print(f"Connected. {len(source.tools)} tool(s) available:")
        for tool in source.tools:
            print(f"  - {tool.name}: {tool.description or ''}".rstrip())
        print()

        address_id = None
        if settings.default_address_label:
            try:
                address_id = await find_saved_address_id(source.call_tool, settings.default_address_label)
            except Exception as exc:  # noqa: BLE001 - fall back to asking per order
                print(f"[couldn't load saved addresses: {exc}]")
            if address_id:
                print(f"Delivering to your saved '{settings.default_address_label}' address by default.\n")
            else:
                print(
                    f"No saved address labelled '{settings.default_address_label}' found; "
                    "the assistant will ask which address to use.\n"
                )

        orchestrator = Orchestrator(
            settings=settings,
            tools=source.tools,
            call_tool=source.call_tool,
            confirm=_confirm,
            default_address_label=settings.default_address_label,
            default_address_id=address_id,
        )

        loop = asyncio.get_event_loop()
        while True:
            try:
                user_input = await loop.run_in_executor(None, input, "you> ")
            except EOFError:
                break
            if not user_input.strip():
                continue
            if user_input.strip().lower() in ("exit", "quit"):
                break

            try:
                reply = await orchestrator.send(user_input)
            except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                print(f"\n[error] {exc}\n")
                continue

            print(f"\nassistant> {reply}\n")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

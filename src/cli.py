"""REPL entrypoint: `python -m src.cli`.

Asks which Swiggy service to use (Food, Instamart, Dineout, ... — whatever
SWIGGY_SERVICES lists), connects to that MCP server as you (OAuth, browser
consent the first time per service), then hands the conversation to that
service's orchestrator turn by turn. `/<service>` switches at any time;
each service keeps its own connection and conversation.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack

from . import config
from .orchestrator import Orchestrator
from .services import open_service

BANNER = """\
Swiggy Conversational Assistant (personal use)
-----------------------------------------------
Services: {services}
Type /<service> to switch (e.g. /{first}), 'exit' or Ctrl-D to quit.
"""


async def _confirm(tool_name: str, arguments: dict, summary: str) -> bool:
    """CLI confirmation gate for order/payment/booking tool calls."""
    print(f"\n[confirmation needed] About to run '{tool_name}':")
    if summary:
        print(f"  {summary}")
    print(f"  details: {arguments}")
    answer = await asyncio.get_event_loop().run_in_executor(
        None, input, "Proceed? [y/N]: "
    )
    return answer.strip().lower() in ("y", "yes")


async def _open_service(
    name: str, settings: config.Settings, stack: AsyncExitStack
) -> Orchestrator:
    print(f"\nConnecting to {name} ({settings.swiggy_services[name]}) ...")
    opened = await open_service(name, settings, stack, confirm=_confirm, notify=print)
    # Names only: Swiggy's full descriptions are pages of instructions
    # meant for the model, not for the terminal.
    print(f"Connected. {len(opened.tool_names)} tool(s): {', '.join(opened.tool_names)}")
    if opened.address_note:
        print(opened.address_note)
    print()
    return opened.orchestrator


def _pick_service(answer: str, services: list[str]) -> str | None:
    answer = answer.strip().lower().lstrip("/")
    if answer.isdigit() and 1 <= int(answer) <= len(services):
        return services[int(answer) - 1]
    return answer if answer in services else None


async def _run() -> None:
    settings = config.load_settings()
    services = list(settings.swiggy_services)
    print(BANNER.format(services=", ".join(services), first=services[0]))

    loop = asyncio.get_event_loop()
    orchestrators: dict[str, Orchestrator] = {}
    current: str | None = None

    async with AsyncExitStack() as stack:
        while True:
            if current is None:
                menu = "  ".join(f"{i}) {s}" for i, s in enumerate(services, 1))
                try:
                    answer = await loop.run_in_executor(None, input, f"Which service? {menu}\n> ")
                except EOFError:
                    break
                if answer.strip().lower() in ("exit", "quit"):
                    break
                current = _pick_service(answer, services)
                if current is None:
                    print(f"Please pick one of: {', '.join(services)}")
                    continue

            if current not in orchestrators:
                try:
                    orchestrators[current] = await _open_service(current, settings, stack)
                except Exception as exc:  # noqa: BLE001 - other services can still work
                    print(f"[couldn't connect to {current}: {exc}]\n")
                    current = None
                    continue

            try:
                user_input = await loop.run_in_executor(None, input, f"you ({current})> ")
            except EOFError:
                break
            text = user_input.strip()
            if not text:
                continue
            if text.lower() in ("exit", "quit"):
                break
            if text.startswith("/"):
                picked = _pick_service(text, services)
                if picked is None:
                    print(f"Unknown service. Available: {', '.join('/' + s for s in services)}")
                else:
                    current = picked
                    if current in orchestrators:
                        print(f"Switched to {current}.\n")
                continue

            try:
                reply = await orchestrators[current].send(text)
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

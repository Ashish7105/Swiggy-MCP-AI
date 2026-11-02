"""Opening a Swiggy service for a conversation, shared by the CLI and the
web UI: connect to its MCP server (OAuth on first use), resolve the default
delivery address if the service delivers to saved addresses, and build an
Orchestrator wired to the caller's confirmation and status callbacks.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

from . import config
from .mcp_client import MCPToolSource, SwiggyMCPConnection
from .orchestrator import (
    ConfirmFn,
    NotifyFn,
    Orchestrator,
    find_address_label,
    load_saved_addresses,
)


@dataclass
class OpenedService:
    orchestrator: Orchestrator
    tool_names: list[str]
    # The saved-address label deliveries go to, or "" if the service
    # doesn't deliver to saved addresses or none matched.
    address_label: str
    # Human-readable note about the address setup, for the UI to show.
    address_note: str


async def _default_address(settings: config.Settings, source: MCPToolSource) -> tuple[dict[str, str], str, str]:
    """Saved addresses, the default label and a note about it, for services
    that deliver to a saved address (they expose get_addresses). Others
    (e.g. Dineout, which searches by area) get none and the model asks."""
    if not any(t.name == "get_addresses" for t in source.tools):
        return {}, "", ""
    if not settings.default_address_label:
        return {}, "", "No default address set; the assistant will ask which address to use."
    try:
        addresses = await load_saved_addresses(source.call_tool)
    except Exception as exc:  # noqa: BLE001 - fall back to asking per order
        return {}, "", f"Couldn't load saved addresses ({exc}); the assistant will ask."
    label = find_address_label(addresses, settings.default_address_label)
    if not label:
        return addresses, "", (
            f"No saved address labelled '{settings.default_address_label}' found for this service; "
            "the assistant will ask which address to use."
        )
    return addresses, label, (
        f"Delivering to your saved '{label}' address by default. Name another saved address in chat to switch."
    )


async def open_service(
    name: str,
    settings: config.Settings,
    stack: AsyncExitStack,
    confirm: ConfirmFn,
    notify: NotifyFn,
) -> OpenedService:
    """Connect to service ``name`` for the lifetime of ``stack``."""
    source = await stack.enter_async_context(
        SwiggyMCPConnection(settings.swiggy_services[name], settings, label=name)
    )
    addresses, label, note = await _default_address(settings, source)
    orchestrator = Orchestrator(
        settings=settings,
        tools=source.tools,
        call_tool=source.call_tool,
        confirm=confirm,
        notify=notify,
        service=name,
        other_services=tuple(s for s in settings.swiggy_services if s != name),
        saved_addresses=addresses,
        default_address_label=label,
    )
    return OpenedService(orchestrator, [t.name for t in source.tools], label, note)

"""Conversational orchestration layer.

This is the only part of the system that understands "conversation":
- Turns a user's natural-language message, plus whatever MCP tools are
  currently available, into a tool-calling request against Groq's
  OpenAI-compatible chat completions API.
- Executes whichever tool(s) the model decides to call, via the MCP
  session.
- Feeds results back to the model until it produces a final
  natural-language reply.
- Enforces a confirmation guardrail before anything that looks like it
  places an order or charges money — COD orders can't be cancelled, per
  Swiggy's own manifest warning, so this is a hard stop, not a suggestion.

It has zero knowledge of what tools Swiggy's Food MCP server actually
exposes. Tool names/schemas are discovered at runtime by mcp_client.py and
passed straight through.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import groq
from groq import AsyncGroq
from mcp import Tool

from . import config

SYSTEM_PROMPT = """\
You are a conversational assistant layered on top of Swiggy's official food \
ordering tools (search restaurants, browse menus, manage a cart, and check \
out). You do not have any food/restaurant knowledge of your own — every \
fact you state (restaurant names, dishes, prices, availability) must come \
from a tool result, never invented.

Each tool's own description carries detailed, authoritative instructions \
for using it correctly — pagination, which field format to reuse \
(variants vs variantsV2), when to re-fetch vs. reuse data already in \
hand, confirmation steps before destructive or money-moving actions, and \
exact wording for sensitive cases (e.g. never claim an order is placed \
while a UPI payment is still pending; never bring up order cancellation \
unless the user asks, and then only point them to the Swiggy app). Follow \
those tool-specific instructions exactly, even where they're more \
specific than the general guidelines below.

Guidelines:
- When the user's request is ambiguous (e.g. "find something spicy"), pick \
  reasonable tool arguments (e.g. a cuisine or search term implying spice) \
  rather than asking a clarifying question, unless you genuinely cannot \
  proceed without more information (e.g. no delivery address is set).
- When the user refers to something by position ("restaurant number 2", \
  "the first one", "that place"), resolve it against the most recent list \
  you returned in this conversation.
- The user sees ONLY the text of your replies — tool results are never \
  shown to them. Whenever a tool returns something the user asked to see \
  (search results, a menu, cart contents, order status), write the \
  relevant items out in your reply; never say "here it is" without \
  listing them. For long lists (e.g. a full menu) show at most ~15 items, \
  grouped by category, and offer to show more.
- Present lists compactly: name, a defining detail or two (cuisine, rating, \
  price range, delivery time if available), and nothing you weren't given.
- Never state a price, ETA, or availability you did not get from a tool \
  result.
- Never write internal IDs (address, restaurant or item IDs) in your \
  replies; refer to things by name and list position. You still have the \
  IDs in the tool results for your own tool calls.
- If a tool call fails or returns an error, explain plainly what went \
  wrong and suggest a next step — don't surface raw error payloads.
- You MUST use the provided tools for anything that touches live data \
  (restaurants, menus, cart contents, orders) — never answer from \
  assumption.
"""

# Smaller models (gpt-oss-20b) assume tool results are rendered to the user,
# as in MCP apps that show them as widgets, and reply "Here's the menu!"
# without listing it — even with the guideline above. Repeating it right
# after a tool result, where it's the last thing the model reads, fixes
# that; it is added per request and never stored in the history.
TOOL_RESULT_REMINDER = (
    "Reminder: the user cannot see tool results, only your reply. Write out "
    "what they asked to see (names and prices, at most ~15 items, grouped), "
    "without internal IDs."
)

# Tools that commit money, place/finalize an order, or delete user data
# require explicit confirmation before we execute them — on top of
# whatever confirmation the tool's own description already asks the model
# for. This is a hard backstop, not a substitute for the model following
# instructions, so it's intentionally narrow and matched against the tool
# *name* only (not description text, which mentions "pay" far too often —
# e.g. get_payment_options and check_payment_status are pure read-only
# lookups that Swiggy's own instructions say to call without asking).
#
# Known Swiggy Food MCP tools that land here: place_food_order,
# confirm_order, delete_address, flush_food_cart. The generic substrings
# ("place_order", "checkout", "submit_order") are a safety net for tools
# we haven't seen yet (Instamart/Dineout, or future Food tools).
CONFIRM_BEFORE_NAME_SUBSTRINGS = (
    "place_order",
    "place_food_order",
    "checkout",
    "submit_order",
    "confirm_order",
    "delete_address",
    "flush_cart",
    "flush_food_cart",
)


def _tool_needs_confirmation(tool: Tool) -> bool:
    name = tool.name.lower()
    return any(s in name for s in CONFIRM_BEFORE_NAME_SUBSTRINGS)


# Swiggy's tool descriptions are extremely thorough — multi-paragraph
# workflows, emoji callouts, edge-case handling — which is great for a
# model with a generous context/rate budget, but 20 of them in full blow
# well past small-tier Groq limits (e.g. 8,000 tokens/minute on
# openai/gpt-oss-20b) before the conversation even starts, and the nested
# parameter schemas (variants/variantsV2/addons on the cart tools) add
# just as much bulk on top.
#
# Rather than blindly truncating Swiggy's text (which can cut off
# mid-thought and loses the parts of the instructions that matter most),
# we hand-write a short, accurate one-liner per known tool — same spirit
# as a human-reviewed API reference — and only fall back to truncating
# the live description for a tool we don't recognize yet (so new/renamed
# Swiggy tools still work, just with rougher descriptions until someone
# adds a curated entry). We also shrink each parameter's own schema to
# just {type, short description, enum} instead of the full JSON Schema,
# which is where a lot of the remaining bulk lives for tools like
# update_food_cart.
#
# This is a token-budget optimization, not a safety mechanism — the hard
# confirmation gate above is keyed on tool *name*, so it's unaffected by
# anything here. But the fine-grained behavioral instructions that used
# to live in Swiggy's long descriptions (don't claim a UPI order is
# placed while PENDING_PAYMENT, don't bring up cancellation unprompted,
# etc.) are now carried by SYSTEM_PROMPT's general guidelines instead of
# per-tool text, so expect a bit less precision on edge cases than with
# the full descriptions.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_addresses": "Get the user's saved Swiggy delivery addresses, newest/most relevant first.",
    "create_address": "Create a new saved delivery address (full address, name, phone, address type).",
    "delete_address": "Permanently delete a saved delivery address by addressId. Irreversible.",
    "search_restaurants": "Search restaurants for delivery by cuisine/query near an address or lat/lng. Returns name, rating, distanceKm, availabilityStatus.",
    "search_menu": "Search for specific dishes/menu items (optionally scoped to one restaurant). Returns items with prices and variant/addon details needed for update_food_cart.",
    "get_restaurant_menu": "Browse a restaurant's full menu by category (compact view: names, prices, veg flag — no customization detail).",
    "get_food_cart": "Get the current food delivery cart: items, prices, totals, valid addons.",
    "update_food_cart": "Add/update items in the food cart for one restaurant. Use the SAME variant format (variants or variantsV2) the item's search_menu result used. Switching restaurants replaces the whole cart.",
    "flush_food_cart": "Clear/empty the entire food delivery cart. Irreversible.",
    "fetch_food_coupons": "Get available coupons/offers for the current cart and restaurant.",
    "apply_food_coupon": "Apply a coupon code to the current cart.",
    "get_food_orders": "Get the user's past/active food order history (not scoped by address).",
    "get_food_order_details": "Get full details (items, pricing, address, status) for one order by orderId.",
    "track_food_order": "Get live status/ETA for an in-progress order, or all active orders if no orderId given.",
    "get_food_delivery_status": "Internal live-ETA poll for an order-success widget; not for general conversational use.",
    "report_error": "File an error report with Swiggy's MCP team, including the relevant IDs from the failed call.",
    "get_payment_options": "Get the live payment methods (UPI apps, desktop QR, COD) available for the current cart. Always call this when the user is ready to pay.",
    "check_payment_status": "Check the status of one in-flight UPI payment (one read, not a polling loop).",
    "place_food_order": "Place the order with an explicit paymentMethod ('UPI' or 'Cash'), only after the user explicitly confirms the cart, address, and payment method. For UPI this returns PENDING_PAYMENT, not a finished order — do not tell the user it's placed yet.",
    "confirm_order": "Finalize an order from PENDING_PAYMENT to PLACED after a successful UPI payment poll (or mark it failed on timeout).",
}


# Even with the short descriptions above, all 20 tools cost ~4,700 tokens
# per request — over half of the 8,000 tokens/minute Groq's free tier
# allows, and every user turn is at least two requests (pick a tool, then
# answer). So each turn only sends the tool groups its wording points at,
# plus the previous turn's groups (so "add that" after a menu still has
# the browse tools). A message that names no topic ("yes", "the second
# one") reuses the previous turn's groups as-is; browsing is the default
# for a first message, since most fresh requests are "find me X".
#
# Tools not listed in any group (new/renamed Swiggy tools) are always
# sent, so they stay reachable without a code change.
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "browse": ("get_addresses", "search_restaurants", "search_menu", "get_restaurant_menu"),
    "cart": ("get_addresses", "get_food_cart", "update_food_cart", "flush_food_cart", "search_menu"),
    "coupons": ("get_food_cart", "fetch_food_coupons", "apply_food_coupon"),
    "checkout": (
        "get_addresses",
        "get_food_cart",
        "get_payment_options",
        "place_food_order",
        "check_payment_status",
        "confirm_order",
    ),
    "orders": ("get_food_orders", "get_food_order_details", "track_food_order", "get_food_delivery_status"),
    "address": ("get_addresses", "create_address", "delete_address"),
    "support": ("report_error",),
}

GROUP_KEYWORDS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "browse": r"\b(find|search|show|menu|restaurants?|eat|hungry|dish(es)?|food|recommend|suggest|details|cheap(est)?|options?)\b",
        "cart": r"\b(cart|add|remove|quantity|qty|basket|increase|decrease|clear|empty|more of)\b",
        "coupons": r"\b(coupons?|offers?|discounts?|promo|deals?)\b",
        "checkout": r"\b(checkout|check out|pay|payment|upi|cash|cod|place|buy|confirm|proceed|order (it|this|that|now))\b",
        "orders": r"\b(track|tracking|status|where is|eta|past orders?|order history|previous orders?|my orders|delivered|arriv\w*)\b",
        "address": r"\b(address(es)?|deliver(y)? to|location)\b",
        "support": r"\b(report|bug)\b",
    }.items()
}


def _groups_for_message(message: str) -> set[str]:
    return {name for name, pattern in GROUP_KEYWORDS.items() if pattern.search(message)}


def _select_tools(tools: list[Tool], groups: set[str]) -> list[Tool]:
    grouped = {name for names in TOOL_GROUPS.values() for name in names}
    wanted = {name for g in groups for name in TOOL_GROUPS[g]}
    return [t for t in tools if t.name in wanted or t.name not in grouped]


# Tool results are the other big token sink: a restaurant menu page is
# ~2,900 tokens and a search ~1,300, and more than half of that is image
# URLs the model never needs. We strip those, cap each result, and keep
# only the previous turn's results (trimmed) in later requests — older
# turns keep just the user message and the final reply.
CURRENT_TURN_TOOL_CHARS = 8000
PREVIOUS_TURN_TOOL_CHARS = 3000
_IMAGE_TAG = re.compile(r"\s*\[image: [^\]]*\]")
_IMAGE_KEYS = re.compile(r"(image|img|icon|thumbnail)", re.IGNORECASE)


# Swiggy's results are written for MCP apps that render a UI widget
# ("Cart widget is displayed — do not repeat the cart items, prices, or
# totals in text."). This CLI has no widget, so those lines would make the
# model hide exactly what the user asked to see; drop them.
_WIDGET_LINE = re.compile(r"^.*\bwidget\b.*(?:\n|$)", re.IGNORECASE | re.MULTILINE)


def _strip_widget_notes(text: str) -> str:
    return _WIDGET_LINE.sub("", text).strip()


def _drop_image_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_image_fields(v) for k, v in value.items() if not _IMAGE_KEYS.search(k)}
    if isinstance(value, list):
        return [_drop_image_fields(v) for v in value]
    return value


def _compact_tool_text(text: str) -> str:
    """Strip image URLs from a tool result. Results are either text with
    ``[image: ...]`` tags or JSON (sometimes followed by more text)."""
    stripped = text.lstrip()
    try:
        data, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return _IMAGE_TAG.sub("", text)
    compact = json.dumps(_drop_image_fields(data), separators=(",", ":"), ensure_ascii=False)
    rest = stripped[end:]
    return compact + ("\n" + _compact_tool_text(rest) if rest.strip() else "")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[… truncated {len(text) - limit} chars; call the tool again if you need the rest]"


# Groq free-tier limits are per model, so when one model is rate limited
# we can often keep going on another. Short per-minute waits are cheaper
# to sit out than a model switch mid-conversation; anything longer (or a
# daily quota) moves to the next configured model.
MAX_RATE_LIMIT_WAIT_S = 20.0
_RETRY_IN = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)
_DAILY_LIMIT = re.compile(r"per day|\((TPD|RPD)\)", re.IGNORECASE)


def _retry_after_seconds(exc: groq.APIStatusError) -> float:
    header = exc.response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = _RETRY_IN.search(str(exc))
    if match:
        return int(match.group(1) or 0) * 60 + float(match.group(2))
    return 60.0


_UNICODE_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _normalize_id(value: str) -> str:
    return value.strip().translate(_UNICODE_DASHES)


_ADDRESS_LINE = re.compile(r"^\s*\d+\.\s*\[(?P<label>[^\]]+)\].*\(ID: (?P<id>[^)\s]+)\)\s*$", re.MULTILINE)
_PAGE_OF = re.compile(r"page (\d+) of (\d+)", re.IGNORECASE)


async def find_saved_address_id(call_tool: ToolCallFn, label: str) -> str | None:
    """Look up a saved address by its label ("Home", "Work", ...) via
    get_addresses, paging through all saved addresses. Lines look like
    ``5. [Home] Name: full address (ID: abc123)``."""
    page = 1
    while True:
        text = _stringify_tool_result(await call_tool("get_addresses", {"page": page}))
        for match in _ADDRESS_LINE.finditer(text):
            if match.group("label").strip().lower() == label.lower():
                return match.group("id")
        pages = _PAGE_OF.search(text)
        if not pages or page >= int(pages.group(2)):
            return None
        page += 1


def _fallback_description(description: str) -> str:
    """For a tool we don't have a curated entry for: take the first
    sentence (bounded to a sane length), falling back to a hard character
    cut if there's no clean sentence boundary nearby.
    """
    text = (description or "").strip()
    if not text:
        return ""
    match = re.match(r"^([^.!?]{10,220}[.!?])", text)
    if match:
        return match.group(1).strip()
    return text[:200].rsplit(" ", 1)[0] + " […]"


def _tool_description(tool: Tool) -> str:
    return TOOL_DESCRIPTIONS.get(tool.name) or _fallback_description(tool.description or "")


def _resolve_json_type(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "string"
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        return non_null[0] if non_null else "string"
    if isinstance(t, str):
        return t
    for key in ("anyOf", "oneOf"):
        if key in schema:
            variants = [v for v in schema[key] if v.get("type") != "null"]
            if variants:
                return variants[0].get("type", "string")
    if "enum" in schema:
        return "string"
    return "string"


def _minimal_param_schema(schema: dict[str, Any] | None, _depth: int = 0) -> dict[str, Any]:
    """Shrink a JSON Schema to just what the model needs to call the tool
    correctly: each property's type, a short description, and enum values
    — dropping the rest (patterns, minLength, examples, etc.), which cost
    tokens without changing what arguments the model produces. Nested
    object/array schemas are shrunk recursively, capped at a shallow depth
    since Swiggy's cart schemas don't nest deeply.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    for prop_name, prop_schema in (schema.get("properties") or {}).items():
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        prop_type = _resolve_json_type(prop_schema)
        entry: dict[str, Any] = {
            "type": prop_type,
            "description": (prop_schema.get("description") or "")[:100],
        }
        if "enum" in prop_schema:
            entry["enum"] = [str(v) for v in prop_schema["enum"]]
        if prop_type == "array" and isinstance(prop_schema.get("items"), dict) and _depth < 2:
            item_schema = prop_schema["items"]
            item_type = _resolve_json_type(item_schema)
            if item_type == "object" and item_schema.get("properties"):
                entry["items"] = _minimal_param_schema(item_schema, _depth + 1)
            else:
                entry["items"] = {"type": item_type}
        properties[prop_name] = entry

    result: dict[str, Any] = {"type": "object", "properties": properties}
    if schema.get("required"):
        result["required"] = schema["required"]
    return result


def mcp_tool_to_openai_tool(tool: Tool) -> dict[str, Any]:
    """Convert an MCP Tool definition into Groq/OpenAI's function-calling
    schema, using a curated short description and a shrunk parameter
    schema to stay inside tight free-tier token limits (see the
    TOOL_DESCRIPTIONS block above for the full rationale).
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": _tool_description(tool),
            "parameters": _minimal_param_schema(tool.input_schema),
        },
    }


ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]
ToolCallFn = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotifyFn = Callable[[str], None]


@dataclass
class Orchestrator:
    """Owns the Groq <-> MCP tool-use loop for one conversation."""

    settings: config.Settings
    tools: list[Tool]
    call_tool: ToolCallFn
    confirm: ConfirmFn
    notify: NotifyFn = print
    default_address_label: str = ""
    default_address_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    _client: AsyncGroq = field(init=False)
    _models: list[str] = field(init=False)
    _model_index: int = field(init=False, default=0)
    _daily_exhausted: set[str] = field(init=False, default_factory=set)
    _previous_groups: set[str] = field(init=False, default_factory=set)
    _known_address_ids: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        # Retries are handled in _complete, where we can switch models
        # instead of only waiting.
        self._client = AsyncGroq(api_key=self.settings.groq_api_key, max_retries=0)
        self._models = list(dict.fromkeys([self.settings.groq_model, *self.settings.groq_fallback_models]))
        system_prompt = SYSTEM_PROMPT
        if self.default_address_id:
            self._known_address_ids.add(self.default_address_id)
            system_prompt += (
                f"\nDelivery address: the user's saved \"{self.default_address_label}\" address "
                "is already selected. Leave addressId out of every tool call — it is filled "
                "in automatically — and never ask the user which address to use. Only if "
                "the user names a different address, call get_addresses and pass that "
                "address's addressId exactly as listed.\n"
            )
        self.history.append({"role": "system", "content": system_prompt})

    def _with_default_address(self, tool: Tool | None, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fill in the default addressId when the model left the location
        out, and replace one it mangled (models copy long IDs badly, e.g.
        swapping "-" for a Unicode hyphen). Never for deleting an address,
        where the ID picks the target."""
        if (
            not self.default_address_id
            or tool is None
            or tool.name.startswith("delete")
            or "addressId" not in ((tool.input_schema or {}).get("properties") or {})
            or arguments.get("latitude") is not None
        ):
            return arguments
        given = _normalize_id(str(arguments.get("addressId") or ""))
        if given in self._known_address_ids:
            return {**arguments, "addressId": given}
        if given:
            self.notify(f"[unrecognized address from the model; using your {self.default_address_label} address]")
        return {**arguments, "addressId": self.default_address_id}

    def _tool_by_name(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    def _messages_for_request(self, shrink: int = 0) -> list[dict[str, Any]]:
        """The history as sent to Groq: full current turn, trimmed previous
        turn, and only user/final-reply text for anything older.

        ``shrink`` is raised each time Groq rejects a request as too large
        for the per-minute budget: 1 drops the previous turn's tool
        results, 2 also caps this turn's tool results, 3 caps them harder
        and drops all earlier conversation text."""
        user_indexes = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        current_start = user_indexes[-1]
        previous_start = user_indexes[-2] if len(user_indexes) > 1 and shrink == 0 else current_start
        current_tool_chars = {2: 2500, 3: 1000}.get(shrink)

        messages = []
        for i, m in enumerate(self.history):
            if i >= current_start:
                if m["role"] == "tool" and current_tool_chars:
                    m = {**m, "content": _truncate(m["content"], current_tool_chars)}
                messages.append(m)
            elif i >= previous_start:
                if m["role"] == "tool":
                    m = {**m, "content": _truncate(m["content"], PREVIOUS_TURN_TOOL_CHARS)}
                messages.append(m)
            elif m["role"] == "system" or (
                shrink < 3
                and (m["role"] == "user" or (m["role"] == "assistant" and "tool_calls" not in m))
            ):
                messages.append(m)
        if messages[-1]["role"] == "tool":
            messages.append({"role": "system", "content": TOOL_RESULT_REMINDER})
        return messages

    def _next_model(self, reason: str) -> bool:
        """Move to the next fallback model that hasn't used up its daily quota."""
        for i in range(self._model_index + 1, len(self._models)):
            if self._models[i] not in self._daily_exhausted:
                self._model_index = i
                self.notify(f"[{reason}; switching to {self._models[i]}]")
                return True
        return False

    async def _complete(self, tools: list[Tool]) -> Any:
        """One chat completion, absorbing Groq's free-tier limits: short
        per-minute waits are sat out, longer ones and daily quotas move to
        the next fallback model, and an oversized request is retried
        smaller (see _messages_for_request)."""
        shrink = 0
        while True:
            model = self._models[self._model_index]
            # gpt-oss reasoning tokens count against the per-minute budget
            # too; picking a tool and summarizing a result don't need much.
            extra = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
            try:
                return await self._client.chat.completions.create(
                    model=model,
                    max_tokens=1024,
                    tools=[mcp_tool_to_openai_tool(t) for t in tools],
                    messages=self._messages_for_request(shrink),
                    **extra,
                )
            except groq.RateLimitError as exc:
                wait = _retry_after_seconds(exc)
                if _DAILY_LIMIT.search(str(exc)):
                    self._daily_exhausted.add(model)
                    if not self._next_model(f"{model} daily quota used up"):
                        raise RuntimeError(
                            "All configured Groq models have used up their daily quota. "
                            "Try again tomorrow, or add another model to GROQ_FALLBACK_MODELS."
                        ) from exc
                elif wait <= MAX_RATE_LIMIT_WAIT_S or not self._next_model(f"{model} rate limited"):
                    self.notify(f"[Groq per-minute limit reached; waiting {wait:.0f}s]")
                    await asyncio.sleep(wait)
            except groq.NotFoundError as exc:
                if not self._next_model(f"{model} not available on this API key"):
                    raise RuntimeError(f"Groq model {model} is not available on this API key.") from exc
            except groq.APIStatusError as exc:
                # 413: this single request is bigger than the per-minute budget.
                if exc.status_code == 413:
                    if shrink < 3:
                        shrink += 1
                        continue
                    raise RuntimeError(
                        f"This request is too large for {model}'s per-minute limit even after "
                        "trimming. Try a shorter request, or start a new session (type 'exit' "
                        "and run it again)."
                    ) from exc
                raise

    async def send(self, user_message: str) -> str:
        """Run one user turn to completion, returning the model's final reply."""
        self.history.append({"role": "user", "content": user_message})

        # Per-minute limits refill quickly, so each turn starts back on the
        # preferred model unless it has used up its daily quota.
        self._model_index = next(
            (i for i, m in enumerate(self._models) if m not in self._daily_exhausted),
            len(self._models) - 1,
        )

        matched = _groups_for_message(user_message)
        if matched:
            groups = matched | self._previous_groups
        else:
            groups = self._previous_groups or {"browse"}
        # Carry only this turn's own topics forward, so groups don't pile
        # up over a long conversation.
        self._previous_groups = matched or groups
        tools = _select_tools(self.tools, groups)

        # Loop: the model may chain multiple tool calls before it has enough
        # information to answer in natural language.
        while True:
            try:
                response = await self._complete(tools)
            except groq.BadRequestError as exc:
                # The model reached for a tool we filtered out this turn;
                # widen to everything rather than fail the turn.
                if "tool" not in str(exc).lower() or len(tools) == len(self.tools):
                    raise
                tools = self.tools
                continue

            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            # Record the assistant turn (content + any tool calls) so the
            # next request has full context, exactly as the API returned it.
            # Groq's message validator rejects "tool_calls": null outright —
            # the key must be omitted entirely on a turn with no tool
            # calls, not set to null, so we only add it when there's
            # something to add.
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = [tc.model_dump() for tc in tool_calls]
            self.history.append(assistant_message)

            if not tool_calls:
                # Some fallback models (e.g. Qwen) inline their reasoning.
                return re.sub(r"<think>.*?</think>\s*", "", message.content or "", flags=re.DOTALL)

            for tool_call in tool_calls:
                name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                tool_def = self._tool_by_name(name)
                arguments = self._with_default_address(tool_def, arguments)
                if tool_def is not None and _tool_needs_confirmation(tool_def):
                    approved = await self.confirm(name, arguments)
                    if not approved:
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "User declined to confirm this action; "
                                "do not retry it without asking again.",
                            }
                        )
                        continue

                try:
                    result = await self.call_tool(name, arguments)
                    text = _stringify_tool_result(result)
                    if name == "get_addresses":
                        self._known_address_ids.update(m.group("id") for m in _ADDRESS_LINE.finditer(text))
                    content = _truncate(_compact_tool_text(_strip_widget_notes(text)), CURRENT_TURN_TOOL_CHARS)
                except Exception as exc:  # noqa: BLE001 - surfaced to the model, not raised
                    content = f"Tool call failed: {exc}"

                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": content,
                    }
                )


def _stringify_tool_result(result: Any) -> str:
    """MCP tool results carry a list of content blocks (text/image/etc).

    We pass text blocks through verbatim and compactly note any non-text
    content (the model doesn't need raw image bytes to describe a menu
    item).
    """
    if hasattr(result, "content"):
        parts = []
        for block in result.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                parts.append(block.text)
            else:
                parts.append(f"[non-text content: {block_type}]")
        return "\n".join(parts) if parts else "(empty result)"
    return json.dumps(result, default=str)

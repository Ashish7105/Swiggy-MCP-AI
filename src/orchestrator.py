"""Conversational orchestration layer.

This is the only part of the system that understands "conversation":
- Turns a user's natural-language message, plus whatever MCP tools the
  connected Swiggy service exposes, into a tool-calling request against an
  LLM (Gemini or Groq) through their OpenAI-compatible chat completions APIs.
- Executes whichever tool(s) the model decides to call, via the MCP
  session.
- Feeds results back to the model until it produces a final
  natural-language reply.
- Enforces a confirmation guardrail before anything the server marks as
  destructive (placing an order, booking, clearing a cart) — COD orders
  can't be cancelled, per Swiggy's own manifest warning, so this is a hard
  stop, not a suggestion.

It has no hardcoded knowledge of any Swiggy service's tools. One
Orchestrator serves one service (Food, Instamart, Dineout, ...); tool
names, descriptions, schemas and safety hints are discovered at runtime by
mcp_client.py and passed straight through.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import openai
from mcp import Tool
from openai import AsyncOpenAI

from . import config

SYSTEM_PROMPT = """\
You are a conversational assistant layered on top of Swiggy's official \
"{service}" service, reached only through the tools you are given. You do \
not have any knowledge of your own about restaurants, products, prices or \
availability — every fact you state must come from a tool result, never \
invented.

Each tool's own description carries detailed, authoritative instructions \
for using it correctly — pagination, which field formats to reuse, when to \
re-fetch vs. reuse data already in hand, confirmation steps before \
destructive or money-moving actions, and exact wording for sensitive \
cases. Follow those tool-specific instructions exactly, even where they're \
more specific than the general guidelines below. Ignore any instruction \
about a UI widget: this app is text-only and shows no widgets.

Guidelines:
- When the user's request is ambiguous (e.g. "find something spicy"), pick \
  reasonable tool arguments rather than asking a clarifying question, \
  unless you genuinely cannot proceed without more information.
- When the user refers to something by position ("number 2", "the first \
  one", "that place"), resolve it against the most recent list you \
  returned in this conversation.
- Never create, edit or delete saved addresses unless the user explicitly \
  asks you to.
- Only add or book exactly what the user chose, from the restaurant or \
  store they chose. If it is unavailable or a tool call fails, say so and \
  ask what to do — never substitute a different item, restaurant or store \
  on your own.
- The user sees ONLY the text of your replies — tool results are never \
  shown to them. Whenever a tool returns something the user asked to see \
  (search results, a menu, a cart, slots, order status), write the \
  relevant items out in your reply; never say "here it is" without \
  listing them. For long lists show at most ~15 items, grouped, and offer \
  to show more.
- Present lists compactly: name, a defining detail or two, and nothing \
  you weren't given.
- Never write internal IDs in your replies; refer to things by name and \
  list position. You still have the IDs in the tool results for your own \
  tool calls.
- If a tool call fails or returns an error, explain plainly what went \
  wrong and suggest a next step — don't surface raw error payloads.
- You MUST use the provided tools for anything that touches live data — \
  never answer from assumption.
{other_services}"""

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

# Tools that commit money, place/finalize an order or booking, or delete
# user data require explicit confirmation before we execute them — on top
# of whatever confirmation the tool's own description asks the model for.
# Swiggy marks these itself with the MCP ``destructiveHint`` annotation
# (on Food: place_food_order, confirm_order, flush_food_cart,
# delete_address), so new services and tools are covered without a code
# change. The name pattern is only a backstop for a tool that arrives
# without annotations.
#
# Creating account records isn't "destructive" by MCP's definition, so
# Swiggy doesn't flag create_address — yet models call it on their own
# when a user merely types their name and number, leaving junk saved
# addresses behind. Anything that creates a record is confirmed too.
_RISKY_NAME = re.compile(r"place|order$|checkout|book|confirm|delete|flush|clear|cancel|pay$", re.IGNORECASE)
_CREATES_RECORD = re.compile(r"^(create|add|save|new)_", re.IGNORECASE)


def _tool_needs_confirmation(tool: Tool) -> bool:
    hints = tool.annotations
    if hints is not None and hints.read_only_hint:
        return False
    if _CREATES_RECORD.match(tool.name):
        return True
    if hints is not None and hints.destructive_hint:
        return True
    # Swiggy doesn't flag every money/booking tool as destructive (Dineout's
    # book_table isn't), so a risky name on a non-read-only tool counts too.
    return bool(_RISKY_NAME.search(tool.name))


# Swiggy's docs: placing an order, checking out and booking are not
# idempotent — after a failure (e.g. a 5xx) the order may still have gone
# through, so check the order/booking list before retrying, or risk a
# duplicate. Tools Swiggy marks destructive and not idempotent get exactly
# that: once a call fails, repeats are refused for the rest of the turn.
UNSAFE_RETRY_BLOCKED = (
    "This action failed, but it may still have gone through on Swiggy's side. Do NOT call it "
    "again. First check the current state with a read-only tool (the order list or booking "
    "status), then tell the user what you found."
)


def _unsafe_to_retry(tool: Tool) -> bool:
    hints = tool.annotations
    if hints is None or not hints.destructive_hint:
        return bool(_RISKY_NAME.search(tool.name))
    return not hints.idempotent_hint


# Swiggy's own tool descriptions (~10k tokens for Food's 20 tools) are the
# authoritative instructions for each tool, so they're sent as-is — minus
# lines about UI widgets this text app doesn't have. Providers with tight
# per-minute token limits (Groq's free tier: ~8k) can't afford that on
# every request, so they get each description's first sentence instead.
COMPACT_DESCRIPTION_PROVIDERS = {"groq"}


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


# Free-tier limits (Gemini and Groq) are per model, so when one model is
# rate limited we can often keep going on another. Short per-minute waits
# are cheaper to sit out than a model switch mid-conversation; anything
# longer (or a daily quota) moves to the next configured model.
# Groq says "Please try again in 1m30s"; Gemini "Please retry in 34.5s"
# and names daily quotas like "GenerateRequestsPerDayPerProjectPerModel".
MAX_RATE_LIMIT_WAIT_S = 20.0
UNAVAILABLE_COOLDOWN_S = 300.0
_RETRY_IN = re.compile(r"(?:try again|retry) in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)
_DAILY_LIMIT = re.compile(r"per day|PerDay|\((TPD|RPD)\)", re.IGNORECASE)


def _retry_after_seconds(exc: openai.APIStatusError) -> float:
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


_ADDRESS_LINE = re.compile(r"^\s*\d+\.\s*\[(?P<label>[^\]]+)\].*\(ID: (?P<id>[^)\s]+)\)\s*$", re.MULTILINE)
_PAGE_OF = re.compile(r"page (\d+) of (\d+)", re.IGNORECASE)


async def load_saved_addresses(call_tool: ToolCallFn) -> dict[str, str]:
    """All saved addresses as {label: addressId} ("Home", "Work", ...), via
    get_addresses, paging through every page. Lines look like
    ``5. [Home] Name: full address (ID: abc123)``. The first address with
    a given label wins."""
    addresses: dict[str, str] = {}
    page = 1
    while True:
        text = _stringify_tool_result(await call_tool("get_addresses", {"page": page}))
        for match in _ADDRESS_LINE.finditer(text):
            addresses.setdefault(match.group("label").strip(), match.group("id"))
        pages = _PAGE_OF.search(text)
        if not pages or page >= int(pages.group(2)):
            return addresses
        page += 1


def find_address_label(addresses: dict[str, str], label: str) -> str | None:
    return next((saved for saved in addresses if saved.lower() == label.lower()), None)


# Saved-address labels are often people's names ("Ashish", "Kajal"), so a
# label alone in a message ("Ashish, 98xxxxxx") is no request to switch.
# It takes a delivery-ish verb as well: "deliver to Kajal", "use Home".
_ADDRESS_INTENT = re.compile(r"\b(deliver\w*|send|ship|address|switch|use|change)\b", re.IGNORECASE)

# Parameters through which tools take the delivery address (Food uses
# addressId, Instamart's update_cart selectedAddressId).
_ADDRESS_PARAM = re.compile(r"^(selected)?addressId$", re.IGNORECASE)


def _label_named_in(message: str, addresses: dict[str, str]) -> str | None:
    """The saved-address label the user's message asks to deliver to, if
    any (longest match wins, so "Ashish Bagalur" beats "Ashish")."""
    if not _ADDRESS_INTENT.search(message):
        return None
    for label in sorted(addresses, key=len, reverse=True):
        if re.search(rf"\b{re.escape(label)}\b", message, re.IGNORECASE):
            return label
    return None


def _first_sentence(description: str) -> str:
    """A description's first sentence (bounded to a sane length), falling
    back to a hard character cut if there's no clean sentence boundary
    nearby. Used for providers that can't afford full descriptions.
    """
    text = (description or "").strip()
    if not text:
        return ""
    match = re.match(r"^([^.!?]{10,220}[.!?])", text)
    if match:
        return match.group(1).strip()
    return text[:200].rsplit(" ", 1)[0] + " […]"


def _tool_description(tool: Tool, compact: bool) -> str:
    text = _strip_widget_notes(tool.description or "")
    return _first_sentence(text) if compact else text


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


# Arguments of tools that need confirmation are mostly opaque IDs
# (spinId, skuId, addressId), useless to a user deciding whether to say
# yes. So those tools get one extra required argument in which the model
# spells out the action in plain words; it's shown in the confirmation and
# removed before the call reaches Swiggy.
SUMMARY_ARG = "confirmation_summary"
_SUMMARY_SCHEMA = {
    "type": "string",
    "description": (
        "For the user's confirmation prompt: exactly what this call will do, in plain words, "
        "using names from earlier tool results — each item with name, size, quantity and price; "
        "totals, address label and payment method when relevant. No IDs."
    ),
}


def mcp_tool_to_openai_tool(tool: Tool, compact: bool = False) -> dict[str, Any]:
    """Convert an MCP Tool definition into the OpenAI function-calling
    schema, with the server's own description (first sentence only when
    ``compact``) and a shrunk parameter schema. Tools that need the user's
    confirmation also ask the model for a plain-language summary.
    """
    parameters = _minimal_param_schema(tool.input_schema)
    if _tool_needs_confirmation(tool):
        parameters["properties"][SUMMARY_ARG] = _SUMMARY_SCHEMA
        parameters["required"] = [*parameters.get("required", []), SUMMARY_ARG]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": _tool_description(tool, compact),
            "parameters": parameters,
        },
    }


# (tool name, arguments as they'll be sent, the model's plain-language
# summary or "") -> whether the user approved.
ConfirmFn = Callable[[str, dict[str, Any], str], Awaitable[bool]]
ToolCallFn = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotifyFn = Callable[[str], None]


@dataclass
class Orchestrator:
    """Owns the LLM <-> MCP tool-use loop for one conversation with one
    Swiggy service."""

    settings: config.Settings
    tools: list[Tool]
    call_tool: ToolCallFn
    confirm: ConfirmFn
    notify: NotifyFn = print
    # Which Swiggy service this conversation talks to, and the others the
    # user can switch to (so the model can point there instead of failing).
    service: str = "food"
    other_services: tuple[str, ...] = ()
    # {label: addressId} of the user's saved addresses, and which label to
    # deliver to until the user names another. Empty label: no default,
    # the model asks the user instead.
    saved_addresses: dict[str, str] = field(default_factory=dict)
    default_address_label: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    _clients: dict[str, AsyncOpenAI] = field(init=False)
    _models: list[str] = field(init=False)
    _model_index: int = field(init=False, default=0)
    _daily_exhausted: set[str] = field(init=False, default_factory=set)
    _active_address_label: str | None = field(init=False, default=None)
    _unavailable_until: dict[str, float] = field(init=False, default_factory=dict)
    _offered_tool_names: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        # Retries are handled in _complete, where we can switch models
        # instead of only waiting. The SDK's default 10-minute timeout
        # would freeze a turn on a stuck model; 30s hands it to the next.
        self._clients = {
            provider: AsyncOpenAI(
                api_key=key, base_url=config.LLM_PROVIDER_URLS[provider], max_retries=0, timeout=30.0
            )
            for provider, key in self.settings.llm_api_keys.items()
        }
        self._models = list(dict.fromkeys(self.settings.llm_models))
        self._active_address_label = find_address_label(self.saved_addresses, self.default_address_label or "")
        other = ""
        if self.other_services:
            switches = ", ".join(f"/{name}" for name in self.other_services)
            other = (
                "- If the user asks for something this service doesn't offer, say so and "
                f"tell them they can switch services by typing {switches}.\n"
            )
        system_prompt = SYSTEM_PROMPT.format(service=self.service, other_services=other)
        if self._active_address_label:
            system_prompt += (
                f"\nDelivery address: the app delivers to the user's saved "
                f"\"{self._active_address_label}\" address and fills it into tool calls "
                "itself. Leave address IDs (addressId, selectedAddressId), latitude and "
                "longitude out of every tool call, and never ask the user for an address, "
                "name or phone number to deliver to. The user switches address by asking to "
                "deliver to another saved address; the app handles that and will tell you.\n"
            )
        self.history.append({"role": "system", "content": system_prompt})

    def _switch_address_if_named(self, user_message: str) -> None:
        """The address is chosen by the user, never by the model: only a
        saved-address label in the user's own message changes it."""
        if not self._active_address_label:
            return
        label = _label_named_in(user_message, self.saved_addresses)
        if label and label != self._active_address_label:
            self._active_address_label = label
            self.notify(f"[delivering to your saved '{label}' address from now on]")
            self.history.append(
                {"role": "system", "content": f"Delivery address is now the user's saved \"{label}\" address."}
            )

    def _with_active_address(self, tool: Tool | None, arguments: dict[str, Any]) -> dict[str, Any]:
        """Force the active address into every tool call that takes one,
        whatever the model passed: models pick other saved addresses on
        their own, or mangle long IDs. Never for deleting an address, where
        the ID picks the target."""
        if not self._active_address_label or tool is None or tool.name.startswith("delete"):
            return arguments
        params = [p for p in ((tool.input_schema or {}).get("properties") or {}) if _ADDRESS_PARAM.match(p)]
        if not params:
            return arguments
        arguments = {k: v for k, v in arguments.items() if k not in ("latitude", "longitude")}
        address_id = self.saved_addresses[self._active_address_label]
        return {**arguments, **{p: address_id for p in params}}

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

    def _usable(self, entry: str) -> bool:
        return entry not in self._daily_exhausted and self._unavailable_until.get(entry, 0) <= time.monotonic()

    def _next_model(self, reason: str) -> bool:
        """Move to the next fallback model that is currently usable."""
        for i in range(self._model_index + 1, len(self._models)):
            if self._usable(self._models[i]):
                self._model_index = i
                self.notify(f"[{reason}; switching to {self._models[i]}]")
                return True
        return False

    async def _complete(self) -> Any:
        """One chat completion, absorbing free-tier limits: short per-minute
        waits are sat out; longer ones, daily quotas, unavailable or
        overloaded models move to the next model in the list; and an
        oversized request is retried smaller (see _messages_for_request)."""
        shrink = 0
        while True:
            entry = self._models[self._model_index]
            provider, model = entry.split(":", 1)
            # gpt-oss reasoning tokens count against Groq's per-minute budget
            # too; picking a tool and summarizing a result don't need much.
            extra = {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}
            compact = provider in COMPACT_DESCRIPTION_PROVIDERS
            request_tools = self.tools
            if self._active_address_label:
                # The app picks the address; with the lookup tool in hand,
                # models still fetch addresses and ask the user to choose.
                request_tools = [t for t in request_tools if t.name != "get_addresses"]
            self._offered_tool_names = {t.name for t in request_tools}
            try:
                return await self._clients[provider].chat.completions.create(
                    model=model,
                    max_tokens=1024,
                    tools=[mcp_tool_to_openai_tool(t, compact) for t in request_tools],
                    messages=_for_provider(self._messages_for_request(shrink), provider),
                    **extra,
                )
            except openai.RateLimitError as exc:
                wait = _retry_after_seconds(exc)
                if _DAILY_LIMIT.search(str(exc)):
                    self._daily_exhausted.add(entry)
                    if not self._next_model(f"{entry} daily quota used up"):
                        raise RuntimeError(
                            "All configured models have used up their daily quota. "
                            "Try again tomorrow, or add another model to LLM_MODELS."
                        ) from exc
                elif wait <= MAX_RATE_LIMIT_WAIT_S or not self._next_model(f"{entry} rate limited"):
                    self.notify(f"[{entry} per-minute limit reached; waiting {wait:.0f}s]")
                    await asyncio.sleep(wait)
            except openai.NotFoundError as exc:
                if not self._next_model(f"{entry} not available on this API key"):
                    raise RuntimeError(f"Model {entry} is not available on this API key.") from exc
            except (openai.APIConnectionError, openai.InternalServerError) as exc:
                # Overloaded (Gemini's 503 "high demand"), timed out or
                # unreachable: skip it for a few minutes rather than paying
                # the wait again on every turn; another model answers now.
                self._unavailable_until[entry] = time.monotonic() + UNAVAILABLE_COOLDOWN_S
                if not self._next_model(f"{entry} unavailable right now"):
                    raise RuntimeError(
                        "All configured models are unavailable right now. Try again in a minute."
                    ) from exc
            except openai.APIStatusError as exc:
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
        self._switch_address_if_named(user_message)
        self.history.append({"role": "user", "content": user_message})

        # Per-minute limits refill quickly, so each turn starts back on the
        # preferred model unless it is out of daily quota or cooling down.
        self._model_index = next(
            (i for i, m in enumerate(self._models) if self._usable(m)),
            len(self._models) - 1,
        )

        empty_retried = False
        declined_this_turn = False
        failed_unsafe: set[str] = set()

        # Loop: the model may chain multiple tool calls before it has enough
        # information to answer in natural language.
        while True:
            response = await self._complete()

            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            # Gemini now and then answers a tool result with an empty
            # message (0 completion tokens). Ask again once rather than show
            # the user a blank reply; don't record the empty turn.
            if not tool_calls and not (message.content or "").strip():
                if not empty_retried:
                    empty_retried = True
                    continue
                return "Sorry, I didn't get a reply from the model. Please ask again."

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

                # Models sometimes call a tool they saw earlier in the
                # conversation but weren't offered this request (e.g. the
                # hidden get_addresses); only offered tools may run.
                if name not in self._offered_tool_names:
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Tool '{name}' is not available here; continue without it.",
                        }
                    )
                    continue

                if name in failed_unsafe:
                    self.history.append(
                        {"role": "tool", "tool_call_id": tool_call.id, "content": UNSAFE_RETRY_BLOCKED}
                    )
                    continue

                tool_def = self._tool_by_name(name)
                summary = str(arguments.pop(SUMMARY_ARG, "") or "").strip()
                arguments = self._with_active_address(tool_def, arguments)
                if tool_def is not None and _tool_needs_confirmation(tool_def):
                    # One "no" covers the rest of the turn: models retry a
                    # declined order right away, which would re-prompt.
                    approved = not declined_this_turn and await self.confirm(name, arguments, summary)
                    if not approved:
                        declined_this_turn = True
                        self.history.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "User declined to confirm this action. Do not retry it; "
                                "tell the user it was not done and ask what they'd like to change.",
                            }
                        )
                        continue

                failed = False
                try:
                    result = await self.call_tool(name, arguments)
                    failed = bool(getattr(result, "is_error", False))
                    text = _stringify_tool_result(result)
                    content = _truncate(_compact_tool_text(_strip_widget_notes(text)), CURRENT_TURN_TOOL_CHARS)
                except Exception as exc:  # noqa: BLE001 - surfaced to the model, not raised
                    failed = True
                    content = f"Tool call failed: {exc}"
                if failed and tool_def is not None and _unsafe_to_retry(tool_def):
                    failed_unsafe.add(name)
                    content += "\n\n" + UNSAFE_RETRY_BLOCKED

                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": content,
                    }
                )


def _for_provider(messages: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Gemini attaches provider-specific data to tool calls (``extra_content``
    carrying its thought signature) that it needs back on later requests;
    other providers may reject the unknown field, so drop it for them."""
    if provider == "gemini":
        return messages
    cleaned = []
    for m in messages:
        if m.get("tool_calls"):
            m = {**m, "tool_calls": [{k: v for k, v in tc.items() if k != "extra_content"} for tc in m["tool_calls"]]}
        cleaned.append(m)
    return cleaned


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

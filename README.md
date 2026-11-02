# Swiggy Conversational Assistant (personal use)

A thin AI orchestration layer in front of Swiggy's official Food MCP
server (`https://mcp.swiggy.com/food`). You talk in plain English; an LLM
(Gemini, with Groq as fallback) decides which of Swiggy's
own tools to call (search, menu, cart, checkout) and turns the result back
into a natural-language reply.

**Scope note:** Swiggy's `swiggy-mcp-server-manifest` states that
third-party app development on top of these servers is not currently
permitted, and auth is per-user OAuth. This project is built and intended
to be run as a **personal client** — the same role Claude Desktop or
Cursor play — against your own Swiggy login, not as a hosted service for
other users. See `ARCHITECTURE.md` for the full reasoning and design.

## What this does and doesn't do

- Does: parse your request, pick the right Swiggy MCP tool(s), call them
  with your authenticated session, and summarize the result conversationally.
- Doesn't: store restaurant/menu data, implement ordering logic, handle
  payments, or talk to Swiggy's APIs directly — all of that is Swiggy's
  MCP server, used as-is.

## Setup

1. **Python 3.11+**, then:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and add at least one LLM API key:
   ```bash
   cp .env.example .env
   ```
   - `GEMINI_API_KEY` — free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
     Used first: its free tier allows far more tokens per minute than Groq's.
   - `GROQ_API_KEY` — free at [console.groq.com/keys](https://console.groq.com/keys).
     Used as a fallback when Gemini is rate limited or overloaded.

   `LLM_MODELS` (optional) sets which models are tried, in order.
3. Run it — in the browser (recommended):
   ```bash
   python -m src.web
   ```
   This opens http://127.0.0.1:8000: service tabs (Food, Instamart,
   Dineout), the chat, live status lines, and a Confirm / Cancel dialog
   before any order or booking. It only listens on your own machine and
   only accepts connections from its own page. Or in the terminal:
   ```bash
   python -m src.cli
   ```
4. Pick a service (Food, Instamart, Dineout). **The first time you use
   each service**, a browser tab opens for you to log into Swiggy and
   approve access (OAuth). Tokens are cached per service in `.mcp_tokens/`
   (gitignored) and refreshed automatically after that.

## Example session

```
Which service? 1) food  2) instamart  3) dineout
> 1
you (food)> I want to eat something spicy
assistant> Here are a few spicy options near you: ...

you (food)> show me the menu of Kanhaji
assistant> Kanhaji's menu includes: ...

you (food)> find paneer dishes
assistant> From what's available: Paneer Tikka (₹...), ...

you (food)> show me the details of restaurant number 2
assistant> That's [restaurant] — ...

you (food)> /instamart
you (instamart)> 2 packets of milk
```

Each service keeps its own connection and conversation; `/food`,
`/instamart`, `/dineout` switch between them. Services come from
`SWIGGY_SERVICES` in `.env`, and each server's tools are discovered at
runtime — nothing about a service's tools is hardcoded.

## Delivery address

For services that deliver to a saved address (they expose `get_addresses`:
Food, Instamart), the assistant looks up your saved Swiggy address labelled
`DEFAULT_ADDRESS_LABEL` (default `Home`) and uses it for every search,
menu, cart and order call, so it doesn't ask each time. Name another
address in chat ("deliver to Work instead") to use that one, or set
`DEFAULT_ADDRESS_LABEL=` (empty) in `.env` to be asked every time.
Dineout searches by area instead, so it asks which city or area you mean.

## Safety guardrail

Any tool the Swiggy server marks as destructive (MCP `destructiveHint`:
placing an order, booking a table, clearing a cart, deleting an address)
pauses and asks you to confirm in the terminal before it runs. Tools
without annotations fall back to a name check (`place`, `checkout`,
`book`, ...). Swiggy's own manifest notes that COD orders
can't be cancelled, so this confirmation step is intentionally not
skippable.

## Adding another Swiggy service

Add it to `SWIGGY_SERVICES` in `.env`, e.g. `...,scenes=https://mcp.swiggy.com/scenes`.
Its tools, descriptions and safety hints are discovered when you first
pick it; no code change is needed.

## Troubleshooting

- **OAuth callback never completes / port in use:** change
  `OAUTH_REDIRECT_PORT` in `.env` to a free port.
- **"redirect URI not whitelisted":** the manifest's whitelisted redirect
  URIs are fixed; use `localhost` with the default port, or check the
  current manifest for which localhost ports/paths are allowed.
- **`[... per-minute limit reached; waiting Ns]` / `[... switching to ...]`:**
  a free-tier LLM limit was hit. Short waits are sat out; longer ones or a
  used-up daily quota move to the next model in `LLM_MODELS`. Nothing is
  wrong — the reply still comes. For no waiting, use a paid tier.
- **Don't run the Swiggy app while using this** — per the manifest,
  concurrent sessions can cause conflicts.

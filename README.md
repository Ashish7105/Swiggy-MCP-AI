# Swiggy Conversational Food Assistant (personal use)

A thin AI orchestration layer in front of Swiggy's official Food MCP
server (`https://mcp.swiggy.com/food`). You talk in plain English; an LLM
on Groq (tool-calling, OpenAI-compatible API) decides which of Swiggy's
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
2. Copy `.env.example` to `.env` and add your own Groq API key
   (get one free at [console.groq.com/keys](https://console.groq.com/keys)):
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and set `GROQ_API_KEY=gsk_...`.
3. Run it:
   ```bash
   python -m src.cli
   ```
4. **First run only:** a browser tab opens for you to log into Swiggy and
   approve access (OAuth). Tokens are cached in `.mcp_tokens/` (gitignored)
   and refreshed automatically after that.

## Example session

```
you> I want to eat something spicy
assistant> Here are a few spicy options near you: ...

you> show me the menu of Kanhaji
assistant> Kanhaji's menu includes: ...

you> find paneer dishes
assistant> From what's available: Paneer Tikka (₹...), ...

you> show me the details of restaurant number 2
assistant> That's [restaurant] — ...
```

## Delivery address

On startup the assistant looks up your saved Swiggy address labelled
`DEFAULT_ADDRESS_LABEL` (default `Home`) and uses it for every search,
menu, cart and order call, so it doesn't ask each time. Name another
address in chat ("deliver to Work instead") to use that one, or set
`DEFAULT_ADDRESS_LABEL=` (empty) in `.env` to be asked every time.

## Safety guardrail

Any tool whose name/description implies placing an order or paying
(`place_order`, `checkout`, `pay`, etc.) pauses and asks you to confirm in
the terminal before it runs. Swiggy's own manifest notes that COD orders
can't be cancelled, so this confirmation step is intentionally not
skippable.

## Extending to Instamart / Dineout

`SwiggyMCPConnection` takes a server URL as a parameter. To add grocery or
table-booking conversations, open a second connection to
`https://mcp.swiggy.com/im` or `/dineout` and merge its tools into the
same `Orchestrator` — see `ARCHITECTURE.md` → "Extensibility".

## Troubleshooting

- **OAuth callback never completes / port in use:** change
  `OAUTH_REDIRECT_PORT` in `.env` to a free port.
- **"redirect URI not whitelisted":** the manifest's whitelisted redirect
  URIs are fixed; use `localhost` with the default port, or check the
  current manifest for which localhost ports/paths are allowed.
- **`[Groq per-minute limit reached; waiting Ns]` / `switching to ...`:**
  Groq's free tier allows ~8,000 tokens/minute per model, and one turn
  uses roughly 3–6k. To stay inside that, each turn sends only the
  Swiggy tools its wording points at, image URLs are stripped from tool
  results, and only the previous turn's results are kept (trimmed). When
  a limit is still hit, short waits are sat out and longer ones (or a
  used-up daily quota) move to the next model in `GROQ_FALLBACK_MODELS`.
  For no waiting at all, use a paid Groq tier.
- **Don't run the Swiggy app while using this** — per the manifest,
  concurrent sessions can cause conflicts.

# Architecture — Swiggy Conversational Food Assistant

## Scope and constraint

This is a **personal-use prototype**, not a hosted third-party product. Swiggy's
MCP manifest (`swiggy-mcp-server-manifest`) states that third-party app
development on top of these servers is not currently permitted, and that
auth is per-user OAuth (you log into *your own* Swiggy account), not a
service-level API key. So this assistant runs locally, as a custom MCP
*client* — the same role Claude Desktop, Cursor, or VS Code play — just with
its own conversational orchestration loop in front of it. It is meant to be
run by you, for you, against your own Swiggy login.

## Principle: thin orchestration, zero reimplementation

We never reimplement restaurant search, carts, or ordering. Swiggy's Food
MCP server (`https://mcp.swiggy.com/food`) already exposes that as MCP
tools. Our code does exactly three things:

1. **Connect** to the MCP server and authenticate (OAuth).
2. **Discover** whatever tools the server exposes at runtime (`list_tools`) —
   we do not hardcode tool names or schemas, because Swiggy can add/change
   tools without notice and the manifest page doesn't publish a frozen tool
   list.
3. **Orchestrate**: take natural-language input, let an LLM (a tool-calling
   model served by Groq, e.g. `llama-3.3-70b-versatile`) decide which
   discovered tool(s) to call and with what arguments, execute them
   through the MCP session, and turn the structured result back into a
   natural-language reply.

Everything else (session/cart state, order history, menus, pricing) lives on
Swiggy's side, behind the MCP tools.

## Components

```
                         ┌──────────────────────────┐
   user (terminal) ───▶  │   cli.py (REPL)           │
                         └────────────┬──────────────┘
                                      │ user turn
                                      ▼
                         ┌──────────────────────────┐
                         │ orchestrator.py           │
                         │  - keeps conversation     │
                         │    history                │
                         │  - calls Groq with        │
                         │    MCP tools as OpenAI-   │
                         │    style function defs    │
                         │  - on tool_calls: calls    │
                         │    mcp_client.call_tool    │
                         │  - feeds tool results back │
                         │    to Groq, loops until    │
                         │    it returns plain text   │
                         └────────────┬──────────────┘
                                      │ list_tools() / call_tool()
                                      ▼
                         ┌──────────────────────────┐
                         │ mcp_client.py             │
                         │  - streamable-HTTP MCP    │
                         │    session to             │
                         │    mcp.swiggy.com/food    │
                         │  - OAuth login (browser,  │
                         │    local callback server) │
                         │  - token cache on disk    │
                         └────────────┬──────────────┘
                                      │ HTTPS (OAuth bearer)
                                      ▼
                         ┌──────────────────────────┐
                         │ Swiggy Food MCP server    │
                         │ (restaurants, menus,      │
                         │  cart, COD checkout)      │
                         └──────────────────────────┘
```

## Why native tool-use instead of custom intent classification

The brief's example turns ("find spicy food", "show restaurant #2", "cheapest
options") are exactly what tool-calling LLMs are built for: the MCP tool
schemas (names, descriptions, JSON-schema parameters) are handed to the
Groq model as OpenAI-style function definitions, and the model decides
which tool(s) to invoke and with what arguments. This avoids a brittle
hand-written NLU layer and keeps the system adaptive if Swiggy changes or
adds tools — we don't need to update our code, only re-fetch
`list_tools()`.

## Conversation-level logic we DO own

A thin layer still belongs to us, because it's about *dialogue*, not about
food:

- **Reference resolution** — "restaurant number 2", "the first one", "that
  place" refer to the last list the model/the tool surfaced. We keep the
  last tool result(s) in the conversation history (as `tool` role
  messages) so the model can resolve the reference itself; we don't
  maintain a separate index ourselves.
- **Session continuity** — one MCP session per CLI run; conversation history
  (and therefore cart/ordering context) persists for the process lifetime.
- **Guardrails before checkout** — any tool whose name/description implies
  placing an order or paying requires an explicit user confirmation step in
  the CLI before the orchestrator is allowed to execute it (see
  `orchestrator.py: CONFIRM_BEFORE_TOOL_SUBSTRINGS`). COD orders can't be
  cancelled per the manifest's own warning, so this confirmation is not
  optional.
- **Error translation** — MCP tool errors (auth expired, tool invocation
  failure) become plain-language messages, not stack traces.

## Auth model

OAuth, user-driven, matching how Claude Desktop/Cursor connect to this same
server:

1. First run: `mcp_client.py` opens your browser to Swiggy's OAuth
   consent screen (localhost redirect, as whitelisted in the manifest).
2. You log in / approve as yourself.
3. Access + refresh tokens are cached locally (`.mcp_tokens/food.json`,
   gitignored) and reused/refreshed on subsequent runs.

No Swiggy credentials or API keys are embedded in the code. This is your
own authenticated session, not a shared backend credential — which is also
why this design stays personal-use rather than a hosted multi-user service.

## Extensibility

Instamart (`/im`) and Dineout (`/dineout`) are structurally identical MCP
servers. `mcp_client.py` takes the server URL as a parameter, so adding
grocery or table-booking conversations later is a matter of connecting a
second `MCPToolSource` and merging its tools into the same tool-use loop —
no architectural change.

## File layout

```
swiggy-ai-assistant/
├── ARCHITECTURE.md          this file
├── README.md                 setup + run instructions
├── requirements.txt
├── .env.example
└── src/
    ├── config.py              env/config loading
    ├── mcp_client.py          MCP session + OAuth + tool discovery/calling
    ├── orchestrator.py        Groq tool-use loop, confirmation guardrail
    └── cli.py                 REPL entrypoint
```

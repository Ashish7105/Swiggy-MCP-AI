# Architecture — Swiggy Conversational Assistant

## Scope and constraint

This is a **personal-use prototype**, not a hosted third-party product.
Swiggy's MCP manifest says third-party app development on these servers is
not currently permitted, and auth is per-user OAuth (you log into *your
own* Swiggy account). So the assistant runs only on your machine, as a
custom MCP *client* — the role Claude Desktop or Cursor play — with its own
conversational loop in front. Swiggy's Builders Club docs allow exactly
this for development: "steps 1 through 5 run on http://localhost, free".

## Principle: thin orchestration, zero reimplementation

We never reimplement restaurant search, carts, ordering or booking. Each
Swiggy service is an MCP server that already exposes those as tools:

| Service   | MCP server                       |
|-----------|----------------------------------|
| Food      | `https://mcp.swiggy.com/food`    |
| Instamart | `https://mcp.swiggy.com/im`      |
| Dineout   | `https://mcp.swiggy.com/dineout` |

Our code does three things:

1. **Connect and authenticate** — OAuth 2.1 + PKCE with one Swiggy login
   shared by every service.
2. **Discover** each server's tools at runtime (`list_tools`): names,
   descriptions, parameter schemas and safety annotations. Nothing about any
   service's tools is hardcoded.
3. **Orchestrate** — hand your message and the tools to an LLM, run the tool
   calls it asks for, and return its plain-language reply.

## Which file does what

```
swiggy-ai-assistant/
├── src/
│   ├── config.py         Settings from .env: LLM keys and model order,
│   │                     the Swiggy services list, default address label,
│   │                     login callback port.
│   ├── mcp_client.py     Talking to Swiggy: the shared OAuth login (browser
│   │                     consent, local callback on :8765, token file),
│   │                     the MCP connection per service, tool discovery.
│   ├── orchestrator.py   The chatbot brain: system prompt, calling the LLM
│   │                     (Gemini, then Groq fallbacks), running the tools
│   │                     it picks, the confirmation gate, delivery-address
│   │                     handling, rate-limit/fallback handling.
│   ├── services.py       Opens one service for a conversation: connect,
│   │                     load the default address, build its Orchestrator.
│   ├── web.py            The web app (python -m src.web): serves the page,
│   │                     one WebSocket for chat, confirmation dialogs.
│   ├── web/index.html    The chat page: service tabs, messages, status
│   │                     lines, Confirm / Cancel dialog.
│   └── cli.py            The terminal version (python -m src.cli).
├── .env                  Your keys and settings (never committed).
├── .env.example          Template for .env.
├── .mcp_tokens/          Your saved Swiggy login (never committed).
├── requirements.txt      Python packages.
├── docs.md               Swiggy Builders Club docs, kept for reference.
└── README.md             Setup and usage.
```

## How one message flows

```
  you (web page / terminal)
        │  "2 packets of milk"
        ▼
  web.py / cli.py ──────────────► services.py (first use of a service:
        │                          connect + default address)
        ▼
  orchestrator.py  Orchestrator.send()
        │ 1. history + your message + the service's tools
        ▼
  LLM (Gemini via its OpenAI-compatible API; Groq if Gemini is limited)
        │ 2. "call search_products(query='milk')"
        ▼
  orchestrator.py  checks the call: offered tool? needs confirmation?
        │          fills in the delivery address
        ▼
  mcp_client.py  ──HTTPS + Swiggy login──►  mcp.swiggy.com/im
        │ 3. tool result (products, prices)
        ▼
  orchestrator.py  trims the result, gives it back to the LLM
        │ 4. LLM writes the reply (or calls another tool, back to 2)
        ▼
  you see the reply
```

The LLM is called in `Orchestrator._complete()`; tools are run in
`Orchestrator.send()`; the MCP connection is `SwiggyMCPConnection` in
`mcp_client.py`.

## Rules we own (conversation, not food)

- **Confirmation before risky actions.** A tool asks you first if Swiggy
  marks it destructive, or it creates a record (`create_…`), or its name
  says it places/books/pays (`place`, `checkout`, `book`, …) and it isn't
  read-only. Dineout's `book_table` isn't marked destructive by Swiggy, so
  the name check is what catches it. The model must also write a
  plain-language summary of the action, shown in the dialog.
- **No blind retries of orders.** Per Swiggy's docs, placing an order,
  checking out and booking aren't idempotent: after a failure the order may
  still have gone through. Such a tool is never called again in the same
  turn; the model is told to check the order/booking status first.
- **The app picks the delivery address, not the model.** For services with
  `get_addresses`, the saved address named by `DEFAULT_ADDRESS_LABEL` is
  filled into every address parameter. It changes only when you ask
  ("deliver to Kajal"). Dineout searches by area instead.
- **Only offered tools run**; tools a model invents or remembers are refused.
- **Text-only app.** Swiggy's results address apps that draw widgets ("cart
  widget is displayed — don't repeat the items"); those lines are removed so
  the model lists what you asked for.

## Auth model

One OAuth 2.1 + PKCE login for all of `https://mcp.swiggy.com` (Swiggy
issues the token for the whole host; its docs: "one OAuth, two MCP URLs").
The client registers itself with Swiggy (dynamic client registration); you
log in with phone + OTP in the browser; the token (5 days) and refresh
token are saved in `.mcp_tokens/mcp.swiggy.com.json` and refreshed
automatically. No Swiggy credentials or keys are in the code.

## LLM and limits

Models are tried in the order of `LLM_MODELS` (default: Gemini Flash,
Gemini Flash-Lite, then Groq models). A model that is rate limited, out of
its daily quota, or overloaded hands the turn to the next one, and the app
tells you in a status line, e.g. `[gemini:gemini-flash-latest daily quota
used up; switching to gemini:gemini-3.5-flash-lite]`.

## Adding a service

Add it to `SWIGGY_SERVICES` in `.env` (e.g. `scenes=https://mcp.swiggy.com/scenes`).
Its tools, descriptions and safety hints are discovered on first use, and
the existing login covers it.

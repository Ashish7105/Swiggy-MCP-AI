Builders Club
HomeDevelopersEnterprisesDocsBlogFAQ
Start Building
HomeStartBuildReferenceOperate
Search...Ctrl+K
Build
Recipes
Recipes5
Order food end-to-end
Order groceries end-to-end
Pay with UPI end-to-end
Book a table
Plan my evening (combined)
Patterns
Agent patterns2
Voice vs chat
Multi-turn cart state
Surfaces
Widgets
Ship
Ship to production
Docs/
Build/
Order food end-to-end
Order food end-to-end
The canonical 7-tool Food journey - from address to placed order to delivery tracking.
The full food-ordering journey across Swiggy's Food MCP server. Pays by COD; to take payment with UPI instead, see Pay with UPI. ₹1000 cart cap. Pseudo-code in TypeScript; the same sequence works in any framework.
The flow
get_addresses
     │
     ▼
search_restaurants ──► get_restaurant_menu
                            │
                            ▼
                       update_food_cart ◄── fetch_food_coupons
                            │                       │
                            ▼                       │
                       get_food_cart ◄── apply_food_coupon
                            │
                            ▼
                       place_food_order
                            │
                            ▼
                       track_food_order
Step 1 - Resolve the delivery address
const addresses = await client.callTool({ name: "get_addresses" });
const home = addresses.data.find((a) => a.label === "Home") ?? addresses.data[0];
if (!home) throw new Error("User has no saved addresses; prompt them to add one.");
If the user has no saved addresses, walk them through create_address — coordinates are optional; the server geocodes from the address.
get_addresses returns label, addressId, and display text - never raw coordinates.
Step 2 - Find restaurants
const restaurants = await client.callTool({
  name: "search_restaurants",
  arguments: { addressId: home.id, query: "biryani" },
});
Check availabilityStatus for each result - only recommend those marked "OPEN". Sort by a mix of distance and rating; always surface distance when picking far restaurants so the user isn't surprised.
Step 3 - Browse the menu
const menu = await client.callTool({
  name: "get_restaurant_menu",
  arguments: { restaurantId: restaurants.data.restaurants[0].id },
});
Menus have categories, items, variants, and add-ons. Use search_menu for keyword search within (or across) restaurants.
Step 4 - Build the cart
await client.callTool({
  name: "update_food_cart",
  arguments: {
    restaurantId: menu.data.restaurantId,
    items: [
      { itemId: menu.data.items[0].id, quantity: 1 },
    ],
  },
});
Cart is tied to a single restaurant. Changing restaurant flushes the cart. Use flush_food_cart explicitly when the user starts over.
Step 5 - Apply a coupon (optional)
const coupons = await client.callTool({ name: "fetch_food_coupons" });
 
// This recipe pays by COD - filter to coupons that don't require online payment
// (paying by UPI instead? see the Pay-with-UPI recipe; online-payment coupons apply there)
const codCoupon = coupons.data.find((c) => !c.requiresOnlinePayment);
 
if (codCoupon) {
  await client.callTool({
    name: "apply_food_coupon",
    arguments: { code: codCoupon.code },
  });
}
Step 6 - Confirm and place the order
const cart = await client.callTool({ name: "get_food_cart" });
 
// Swiggy v1: hard ₹1000 cap on Builders Club orders
if (cart.data.total > 1000) {
  throw new Error("Cart exceeds ₹1000 cap - ask user to reduce items.");
}
 
// Surface to the user before placing
// "Your order is: <items>. Total ₹<total>. Place now? (yes / no)"
 
const order = await client.callTool({
  name: "place_food_order",
  arguments: { paymentMethod: "COD" },
});
Critical: place_food_order is not idempotent. If it fails with 5xx, call get_food_orders to check if the order actually placed before retrying. See ship to production.
Step 7 - Track the order
const status = await client.callTool({
  name: "track_food_order",
  arguments: { orderId: order.data.orderId },
});
 
// Poll no faster than every 10 seconds; delivery-partner ETA updates arrive at that cadence
Full agent prompt
Good system prompt for the agent driving this flow:
You help users order food on Swiggy. Always resolve the user's saved address via get_addresses before searching. Only recommend restaurants with availabilityStatus: "OPEN". Confirm the cart and total with the user before calling place_food_order - that call places a real order. This recipe pays by COD; filter coupons to those not requiring online payment. (To take UPI payment, follow the Pay-with-UPI recipe instead.) Never exceed ₹1000 cart total.
What can go wrong
Until the symbolic error-code registry ships (see errors), classify by error.message text and HTTP status. Expect:
Restaurant closed between search and order → re-run search_restaurants.
Coupon requires online payment → not usable on the COD path; filter upstream, or pay with UPI.
Minimum order not met → prompt user to add items.
Upstream shedding / timeout → exponential backoff; capacity questions go to rate-limits.
← PreviousBuildNext →Order groceries end-to-end
On this page
The flow
Step 1 - Resolve the delivery address
Step 2 - Find restaurants
Step 3 - Browse the menu
Step 4 - Build the cart
Step 5 - Apply a coupon (optional)
Step 6 - Confirm and place the order
Step 7 - Track the order
Full agent prompt
What can go wrong
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.
Swiggy Builders Club - MCP Partnership Program
Order food end-to-end · Swiggy Builders Club

Builders Club
HomeDevelopersEnterprisesDocsBlogFAQ
Start Building
HomeStartBuildReferenceOperate
Search...Ctrl+K
Build
Recipes
Recipes5
Order food end-to-end
Order groceries end-to-end
Pay with UPI end-to-end
Book a table
Plan my evening (combined)
Patterns
Agent patterns2
Voice vs chat
Multi-turn cart state
Surfaces
Widgets
Ship
Ship to production
Docs/
Build/
Order groceries end-to-end
Order groceries end-to-end
Full Instamart journey - find products, build a cart, checkout, track delivery.
Instamart is Swiggy's quick-commerce grocery service across 1000+ Indian cities. Same shape as Food (discover → cart → order → track), different catalogue.
The flow
get_addresses
     │
     ▼
search_products ──► update_cart ──► get_cart ──► checkout ──► track_order
     ▲                  │
     └──────────────────┘
     your_go_to_items (bypass search)
Step 1 - Resolve the delivery address
const addresses = await client.callTool({ name: "get_addresses" });
const home = addresses.data.find((a) => a.label === "Home") ?? addresses.data[0];
If the user has no addresses, walk them through create_address.
Step 2 - Find products (or reorder)
Two paths. For quick reorders:
const goTo = await client.callTool({
  name: "your_go_to_items",
  arguments: { addressId: home.id },
});
// goTo.data has frequently-ordered SKUs - present as one-tap add
For search:
const results = await client.callTool({
  name: "search_products",
  arguments: { addressId: home.id, query: "bananas" },
});
Each product returns one or more variations with their own spinId (the SKU-level identifier). You add variations to the cart, not the parent product.
The response may also include a similarProducts array — related items shown when the catalogue API finds relevant alternatives. Present these in a separate "Similar Items" section below the main results.
Step 3 - Build the cart
await client.callTool({
  name: "update_cart",
  arguments: {
    items: [
      { spinId: results.data.products[0].variants[0].spinId, quantity: 2 },
    ],
  },
});
Swapping address mid-cart? Don't. Run clear_cart first to avoid cross-address SKU mismatches.
Step 4 - Review the cart
const cart = await client.callTool({ name: "get_cart" });
// cart.data has items[], bill breakdown, payment methods available
Check ADDRESS_NOT_SERVICEABLE or MIN_ORDER_NOT_MET errors; Instamart has a ₹99 minimum and service-area restrictions.
Step 5 - Checkout
const order = await client.callTool({
  name: "checkout",
  arguments: { paymentMethod: "COD" },
});
This uses COD. To take payment with UPI, call get_payment_options first and pass the picked method into checkout (paymentMethod:"UPI" + intentApp/generateUPIQR) - see the Pay with UPI recipe.
Same non-idempotency rule as Food: if checkout 5xxs, check get_orders before retrying.
Step 6 - Track
const status = await client.callTool({
  name: "track_order",
  arguments: { orderId: order.data.orderId },
});
// ETA typically 10-20 min post-checkout
Poll no faster than every 10s.
Agent prompt
You help users shop on Swiggy Instamart. Start by resolving the user's saved address. Offer your_go_to_items for quick reorders; use search_products for new queries. Always confirm the cart and total before checkout. When the user is ready to pay, call get_payment_options — both UPI (scan-QR / app intent) and Cash are supported; Cash is the fallback when UPI isn't available.
Common errors
Until the symbolic error.code registry ships (see errors), classify by error.message text. Expect:
Item out of stock at this address → suggest alternatives from search_products.
Address not serviceable → Instamart doesn't deliver here; ask for another address or offer Food.
Minimum order not met (cart under ₹99) → prompt user to add items.
Cart expired / abandoned → rebuild the cart.
← PreviousOrder food end-to-endNext →Pay with UPI end-to-end
On this page
The flow
Step 1 - Resolve the delivery address
Step 2 - Find products (or reorder)
Step 3 - Build the cart
Step 4 - Review the cart
Step 5 - Checkout
Step 6 - Track
Agent prompt
Common errors
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.
Swiggy Builders Club - MCP Partnership Program
Order groceries end-to-end · Swiggy Builders Club

Builders Club
HomeDevelopersEnterprisesDocsBlogFAQ
Start Building
HomeStartBuildReferenceOperate
Search...Ctrl+K
Build
Recipes
Recipes5
Order food end-to-end
Order groceries end-to-end
Pay with UPI end-to-end
Book a table
Plan my evening (combined)
Patterns
Agent patterns2
Voice vs chat
Multi-turn cart state
Surfaces
Widgets
Ship
Ship to production
Docs/
Build/
Book a table
Book a table
Dineout journey - find a restaurant, check availability, reserve.
Dineout is Swiggy's table-reservation surface across 50+ Indian cities. The flow is compact: find → check slots → book → confirm.
The flow
search_restaurants_dineout ──► get_restaurant_details
                                        │
                                        ▼
                               get_available_slots ──► book_table ──► get_booking_status
Step 1 - Search
Dineout search takes the coordinates of the city or area the user named. There is no saved-address lookup. If the user says "near me" without a place, ask which city or area.
const restaurants = await client.callTool({
  name: "search_restaurants_dineout",
  arguments: {
    latitude: 12.9716,
    longitude: 77.5946,
    query: "italian",
  },
});
Results include availability status, offers, cuisines, and price range. Filter to restaurants where availability is "AVAILABLE" before presenting.
Step 2 - Dig into details
const details = await client.callTool({
  name: "get_restaurant_details",
  arguments: { restaurantId: restaurants.data.restaurants[0].id },
});
// details.data: ratings, amenities, menu images, exclusive Dineout deals
Step 3 - Check available slots
const slots = await client.callTool({
  name: "get_available_slots",
  arguments: {
    restaurantId: details.data.id,
    date: "2026-05-01",
    guestCount: 4,
  },
});
// slots.data has 7-day forward availability, broken into breakfast/lunch/dinner bands
Surface slot times in the user's timezone (all restaurants are in India; IST applies).
Step 4 - Book
const booking = await client.callTool({
  name: "book_table",
  arguments: {
    restaurantId: details.data.id,
    slotId: slots.data.slots[0].slotId,
    guestCount: 4,
  },
});
Important: book_table is not idempotent. On 5xx, call get_booking_status with the restaurant and slot before retrying.
Step 5 - Confirm
const status = await client.callTool({
  name: "get_booking_status",
  arguments: { bookingId: booking.data.bookingId },
});
Send the user the confirmation number and the restaurant's address.
Agent prompt
You help users book restaurant tables on Swiggy Dineout. Ask for the city or area if the user has not named one, then search with its latitude and longitude. Always confirm slot date, time, and party size with the user before book_table. Show restaurant details (amenities, deals) before asking for slot confirmation.
Common errors
SLOT_UNAVAILABLE → slot filled; refetch availability and offer alternatives.
RESTAURANT_NOT_BOOKABLE → restaurant isn't Dineout-enabled; offer dine-in walk-in guidance or a Food order instead.
BOOKING_WINDOW_CLOSED → outside booking hours; present next available day.
Full catalogue: errors.
← PreviousPay with UPI end-to-endNext →Plan my evening (combined)
On this page
The flow
Step 1 - Search
Step 2 - Dig into details
Step 3 - Check available slots
Step 4 - Book
Step 5 - Confirm
Agent prompt
Common errors
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.
Swiggy Builders Club - MCP Partnership Program
Book a table · Swiggy Builders Club

Builders Club
HomeDevelopersEnterprisesDocsBlogFAQ
Start Building
HomeStartBuildReferenceOperate
Search...Ctrl+K
Build
Recipes
Recipes5
Order food end-to-end
Order groceries end-to-end
Pay with UPI end-to-end
Book a table
Plan my evening (combined)
Patterns
Agent patterns2
Voice vs chat
Multi-turn cart state
Surfaces
Widgets
Ship
Ship to production
Docs/
Build/
Plan my evening (combined)
Plan my evening (combined)
One user ask, two MCP servers - Food delivery and Dineout reservations composed in a single agent turn.
A fun showcase of MCP's tool-composition strength: the user says "plan my evening for 4 - dinner out, dessert delivered later", and your agent fans out across two Swiggy servers.
The ask
"Plan my evening for Friday. I want dinner out with 3 friends around 8pm, something Italian in Indiranagar. Then order dessert to my place for 10pm."
This requires both Dineout (for the reservation) and Food (for the delivery). Both servers share the underlying Swiggy session - one OAuth, two MCP URLs.
Connect both servers
Most frameworks allow multiple MCP servers side-by-side. Example with OpenAI Agents SDK:
const dineout = new MCPServerStreamableHttp({
  url: "https://mcp.swiggy.com/dineout",
  requestInit: { headers: { Authorization: `Bearer ${token}` } },
});
const food = new MCPServerStreamableHttp({
  url: "https://mcp.swiggy.com/food",
  requestInit: { headers: { Authorization: `Bearer ${token}` } },
});
 
const agent = new Agent({
  name: "EveningPlanner",
  instructions: "Plan the user's evening using both servers.",
  mcpServers: [dineout, food],
});
Tool names don't collide - search_restaurants (Food) and search_restaurants_dineout are distinct.
What the agent does
Internally, the model orchestrates the two flows in parallel:
                  ┌──────────────────────────┐
                  │  User: Plan my evening    │
                  └─────────────┬─────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
   (user names a city/area)             get_addresses (Food)
              │                                   │
              ▼                                   │
   search_restaurants_dineout                     │
      (query="italian",                           │
       lat/lng of the city/area)                  │
              │                                   │
              ▼                                   │
   get_restaurant_details                         │
              │                                   │
              ▼                                   │
   get_available_slots                            │
      (date=Friday, guestCount=4)                 │
              │                                   │
              ▼                                   │
   book_table                          (later, after dinner recommendation)
                                                  │
                                                  ▼
                                        search_restaurants
                                           (query="gelato",
                                            addressId=home)
                                                  │
                                                  ▼
                                        get_restaurant_menu
                                                  │
                                                  ▼
                                        update_food_cart
                                                  │
                                                  ▼
                                        place_food_order (scheduled 10pm)
Agent prompt for this pattern
You compose Swiggy Dineout and Swiggy Food tools to plan user evenings. When the user asks for a restaurant reservation AND food delivery in one request, handle them sequentially: reservation first (so they see slot options early), then dessert/delivery second. Always confirm reservation details and food cart separately before calling book_table and place_food_order.
Handle auth expiry across both servers
If one server returns 401, your session is gone for both. Re-run the OAuth flow once, update the bearer for both clients, retry.
Gotchas
Scheduling: place_food_order places orders for immediate delivery. Swiggy Food doesn't support future-scheduled delivery in v1 - if the user wants dessert at 10pm exactly, your agent needs to remind them / place the order at the right time.
Address vs location: Dineout takes lat/lng of the city or area; Food uses addressId. They're different scopes. Don't try to pass an addressId to Dineout search.
Cart conflicts: Food cart is per-restaurant. If your agent adds to cart, then searches a different restaurant, the cart will be flushed. Surface that explicitly.
← PreviousBook a tableNext →Voice vs chat
On this page
The ask
Connect both servers
What the agent does
Agent prompt for this pattern
Handle auth expiry across both servers
Gotchas
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.
Swiggy Builders Club - MCP Partnership Program
Plan my evening (combined) · Swiggy Builders Club

Builders Club
Home
Developers
Enterprises
Docs
Blog
FAQ

Start Building
Home
Start
Build
Reference
Operate

Search...
Ctrl+K
Start
What is Swiggy MCP?
Developer

Developer
1
Enterprise

Enterprise
1
Consumer

Consumer
1
Auth
Authenticate
Coding Agents
Coding agents
Docs
/
Start
/
What is Swiggy MCP?
What is Swiggy MCP?
What Swiggy Builders Club offers, who it's for, and when to use it.

Swiggy Builders Club exposes Swiggy's commerce platform as MCP servers - the open standard (Model Context Protocol) that AI agents speak to external tools. One protocol, four servers, 66 tools, zero vendor lock-in.

The four servers
Server	Endpoint	What it does	Tools
Food	mcp.swiggy.com/food	Restaurant discovery, menus, ordering, tracking	20
Instamart	mcp.swiggy.com/im	Quick-commerce grocery	19
Dineout	mcp.swiggy.com/dineout	Table reservations	12
Scenes	mcp.swiggy.com/scenes	Events and shows - discovery and ticket booking	15
Each server is independent. Wire one, some, or all four - they don't share carts, orders, or sessions.

Who it's for
Agent developers building with OpenAI Agents SDK, Anthropic SDK, LangGraph, Vercel AI SDK, Mastra, PydanticAI, CrewAI, or Google ADK. You ship an agent that talks to Swiggy as a tool provider.
Agent platforms running at scale - voice assistants, in-app agents, conversational commerce - that broker Swiggy on behalf of many end users. You need delegated auth and production rate limits.
AI-client users of Claude Desktop, ChatGPT, Cursor, VS Code, or Windsurf who want Swiggy available as a tool inside their existing client. No code - just a config paste.
When MCP fits (and when it doesn't)
MCP shines when an agent needs to discover and reason across multiple tools at runtime. The LLM sees tool schemas, decides which to call, orchestrates multi-step flows like search → menu → cart → checkout. That's Swiggy's sweet spot.

If you're embedding a fixed Swiggy widget into a SaaS product without any agent involvement - a "reorder my last Instamart basket" button - you want Swiggy's regular APIs, not MCP. This site is MCP-only.

What you get out of the box
OAuth 2.1 with PKCE - the same auth flow Claude Desktop and Cursor use natively.
Streamable HTTP transport - one URL per server, standard JSON-RPC.
Session auth - no passing user credentials as tool arguments.
Stable error taxonomy - canonical error codes your code can branch on.
Versioning contract - 6-month deprecation windows (see versioning).
What you're signing up for
India-only user base - Swiggy serves Indian consumers. No cross-border data flow, no US/EU residency.
Whitelist onboarding - production access is invite-based today; see Access.
Partner contract - SLA, rate limits, data handling described in Operate. Some clauses are negotiated per-partner.
Where to go next
Build your first agent: Developer quickstart.
Running an agent platform: Enterprise onboarding.
Just want Swiggy in Claude Desktop: Consumer install.
Poke at the tool catalogue: Reference.
← Previous
Start
Next →
Developer quickstart
On this page
The four servers
Who it's for
When MCP fits (and when it doesn't)
What you get out of the box
What you're signing up for
Where to go next
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.

Instamart
Swiggy Food
Swiggy Dineout
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.

Swiggy Builders Club - MCP Partnership Program

What is Swiggy MCP? · Swiggy Builders Club
Developer quickstart
Zero to first successful Swiggy tool call, self-serve.
This is the self-serve path. You'll ship working agent code that calls a Swiggy MCP tool against a staging endpoint. You will move through account access, framework install, OAuth, and a first tool call.
Building with a coding agent?
These docs are agent-friendly - point Claude Code, Cursor, or Windsurf at them and let your agent read the source of truth. Click here to set it up.
→
You don't need approval to start - steps 1 through 5 below run on http://localhost, free. You only apply for access when you're ready to hit production endpoints. Tip: fill out the form while you build, and include a demo video to speed up review.
1. Understand what you're building
Every Swiggy MCP tool call goes through the same loop:
Your agent picks a tool from the server's catalogue (e.g. search_restaurants).
The MCP client in your agent framework sends a JSON-RPC call to mcp.swiggy.com/{server}.
The server authenticates the session, runs the tool, returns { success, data }.
Your agent reads the result and decides what to call next.
Four servers, independent per URL: Food (/food), Instamart (/im), Dineout (/dineout), Scenes (/scenes).
2. Apply for production access (when you're ready)
You don't need this to start - everything below works on http://localhost without approval. Apply only when you want to take your integration live. Production access is reviewed today, so record a short video of your agent flow working end-to-end and include the link with your application; it dramatically speeds up review.
Apply at /access with:
Integration name and organization
Redirect URIs for OAuth (exact-match, HTTPS - http://localhost allowed for dev)
Which servers you'll call - any of food, instamart, dineout, scenes (v1 scopes are mcp:tools, mcp:resources, mcp:prompts)
Expected volume and use case
A demo video link (Loom, Drive, YouTube unlisted) - or email it to builders@swiggy.in
You'll get staging access; production follows once your staging integration is green. Your MCP client registers itself via Dynamic Client Registration - no client identifier to apply for. See Access for the full flow.
3. Pick a framework
Any MCP-compatible framework works. Pick what you already use; if you're starting fresh, OpenAI Agents SDK and Vercel AI SDK have the lowest-friction MCP story in 2026.
Supported frameworks (recipes in Build an agent):
OpenAI Agents SDK (TypeScript + Python)
Anthropic SDK - native MCP connector
LangGraph / LangChain (via langchain-mcp-adapters)
Vercel AI SDK 6 (experimental_createMCPClient)
Mastra, PydanticAI, CrewAI, Google ADK
Raw MCP client (@modelcontextprotocol/sdk or mcp Python package)
4. Complete OAuth
OAuth 2.1 with PKCE. See Authenticate for the full endpoint walkthrough. Most frameworks handle this for you - you paste the auth URL into your config, a browser window opens for phone + OTP, and you're back.
5. Make your first tool call
Follow the framework recipe at Build an agent. The minimal first call:
const result = await client.callTool({
  name: "get_addresses",
  arguments: {},
});
If you see a user's saved addresses, you're wired up. Now try search_restaurants with an addressId from that response. From there, the world is yours.
6. Build something real
Pick a recipe:
Order food end-to-end - 7 tools, full journey, COD payment.
Order groceries end-to-end - Instamart cart + checkout.
Book a table - Dineout availability + reservation.
Combined: plan my evening - food + dineout in one agent turn.
What's next
Build an agent - per-framework recipes with working code.
Ship to production - retries, observability, go-live checklist.
← PreviousWhat is Swiggy MCP?Next →Build an agentBuilders Club
HomeDevelopersEnterprisesDocsBlogFAQ
Start Building
HomeStartBuildReferenceOperate
Search...Ctrl+K
Start
What is Swiggy MCP?
Developer
Developer1
Enterprise
Enterprise1
Consumer
Consumer1
Auth
Authenticate
Coding Agents
Coding agents
Docs/
Start/
Developer/
Build an agent
Build an agent
Working code to wire Swiggy MCP into the framework you already use.
Swiggy MCP speaks standard streamable HTTP. Every major agent framework in 2026 has first-class MCP support. Pick your framework, paste the connector, give your agent access to 51 Swiggy tools.
Swiggy MCP is OAuth 2.1 + PKCE - there is no static API key. SDK support for the flow splits into two camps:
Native authProvider support (raw MCP TS / Python SDKs, OpenAI Agents JS, Vercel AI SDK 6, Mastra) - pass an OAuth client provider and the SDK runs PKCE against Swiggy's /.well-known/oauth-protected-resource and /.well-known/oauth-authorization-server automatically.
Bearer-header only (OpenAI Agents Python, LangChain MCP adapters, PydanticAI, CrewAI, Google ADK, Anthropic hosted MCP connector) - the SDK has no OAuth hook, so you obtain an access token via the Authenticate flow and forward it as Authorization: Bearer <token>.
The snippets below assume:
swiggyOAuthProvider / swiggy_oauth_provider - your implementation of the MCP SDK's OAuthClientProvider interface, wrapping the Authenticate flow. The Mastra tab shows a ready-made one via MCPOAuthClientProvider.
getSwiggyAccessToken() - your helper that runs the Authenticate flow and returns a fresh Bearer token. Re-run on 401.
Refresh tokens are supported; on expiry or a 401, use the refresh token to get a new access token, and fall back to re-running authorization only if the refresh fails.
OpenAI Agents SDKAnthropic SDKLangGraphVercel AI SDKMastraPydanticAICrewAIGoogle ADKRaw MCP client
import { Agent, Runner, MCPServerStreamableHttp } from "@openai/agents";
import { swiggyOAuthProvider } from "./swiggy-oauth"; // implements OAuthClientProvider
 
const swiggyFood = new MCPServerStreamableHttp({
  url: "https://mcp.swiggy.com/food",
  authProvider: swiggyOAuthProvider,
});
 
const agent = new Agent({
  name: "FoodOrderingAgent",
  instructions: "Help users order food on Swiggy. Always call get_addresses first.",
  mcpServers: [swiggyFood],
});
 
await swiggyFood.connect();
const result = await Runner.run(agent, "Order biryani to my home address.");
console.log(result.finalOutput);
Python (the agents SDK doesn't expose an OAuth hook today - pass a Bearer token in headers):
import os
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp
 
token = await get_swiggy_access_token()  # your OAuth helper
 
swiggy_food = MCPServerStreamableHttp(
    params={
        "url": "https://mcp.swiggy.com/food",
        "headers": {"Authorization": f"Bearer {token}"},
    },
)
 
agent = Agent(
    name="FoodOrderingAgent",
    instructions="Help users order food on Swiggy. Always call get_addresses first.",
    mcp_servers=[swiggy_food],
)
 
await swiggy_food.connect()
result = await Runner.run(agent, "Order biryani to my home address.")
print(result.final_output)
Handling expired tokens
Access tokens live 5 days. When a call returns 401 (or JSON-RPC -32001), refresh the access token with the stored refresh_token and retry; re-run the full OAuth flow only if the refresh itself fails. Most frameworks expose a hook for this; for raw clients:
async function callWithReauth<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e: any) {
    // Auth failures surface as HTTP 401 or JSON-RPC -32001 at the transport layer.
    const rpcCode = e?.body?.error?.code ?? e?.code;
    if (e?.status === 401 || rpcCode === -32001) {
      try {
        await refreshSession(); // POST /auth/token with grant_type=refresh_token
      } catch {
        await reAuthenticate(); // full OAuth flow
      }
      return fn();
    }
    throw e;
  }
}
See Authenticate for the full OAuth walkthrough.
Wire more than one Swiggy server
Each server is independent - connect multiple if your agent needs to span domains:
mcpServers: [
  { url: "https://mcp.swiggy.com/food" },
  { url: "https://mcp.swiggy.com/im" },
  { url: "https://mcp.swiggy.com/dineout" },
  { url: "https://mcp.swiggy.com/scenes" },
]
Tool names are unique across servers, so your agent can dispatch across all 66 public tools without conflict.
Where to go next
Recipes - end-to-end journeys for food, grocery, dineout, and combined flows.
Agent patterns - voice vs chat response shaping, multi-turn state.
Reference - every tool, every parameter.
Ship to production - retries, observability, go-live checklist.
← PreviousDeveloper quickstartNext →Power an agent platform
On this page
Handling expired tokens
Wire more than one Swiggy server
Where to go next
Builders Club
Cook on Swiggy's MCP platform. Open to developers, startups, and enterprises.
builders@swiggy.in
Program
For Developers
For Enterprises
How It Works
Benefits
Resources
Guidelines
FAQ
Apply
llms.txt
Legal
Privacy Policy
Terms and Conditions
© 2026 Swiggy. All rights reserved.
Swiggy Builders Club - MCP Partnership Program
Build an agent · Swiggy Builders Club
Swiggy
swiggy-mcp-server-manifest
Repository navigation
Code
Issues
32
 (32)
Pull requests
1
 (1)
Agents
Discussions
Actions
Security and quality
Insights
Swiggy
swiggy-mcp-server-manifest
Public
Go to file
t
T
soumyakant2-swiggy
soumyakant2-swiggy
Merge pull request #150 from Swiggy/docs/move-builders-section-top
6028add
 · 
last week
Name		
README.md
docs: move 'Build with Swiggy MCP' section to top of README
last week
Repository files navigation
README
Swiggy MCP Server
Order food, groceries and more through intelligent MCP server integrations.

Build with Swiggy MCP
Builders Website - Learn more about building on the Swiggy MCP platform: http://mcp.swiggy.com/builders/
Access Request Form - Apply for builder access here: http://mcp.swiggy.com/builders/access/
ChatGPT Plugin
The Swiggy MCP plugin is currently available on ChatGPT for Swiggy Food only. Users can discover restaurants, browse menus, manage their cart, and place food orders directly from ChatGPT.

Plugin Link: Swiggy Food on ChatGPT

Supported Features
Swiggy Food
Food you 🧡, on time

Restaurant Search - Discover restaurants by cuisine, name, or dish

Menu Browsing - Explore menus, prices, and item details

Cart Management - Add items, customize orders, and view bill breakdown

Food Ordering - Place orders seamlessly with order tracking support (currently supports COD only)

Instamart
Get groceries & more, delivered in mins!

Product Search - Find products by name, category, or brand at your delivery location

Cart Management - View cart items, pricing, and bill breakdown with detailed cost analysis

Order Placement - Seamlessly checkout and place grocery orders with instant confirmation (currently supports COD only)

Dineout
Reserve tables at your favorite restaurants instantly.

Restaurant Discovery - Find nearby restaurants based on your location and preferences

Restaurant Details - Browse through detailed menus with prices, ratings, and exclusive offers

Slot Availability - Check available booking time slots for your preferred date

Table Booking - Reserve tables instantly with seamless booking support (supports free bookings only)

Getting Started
Supported OAuth Redirect URIs
The following redirect URIs are whitelisted for MCP authentication:

claude://claude.ai/settings/connectors
https://chatgpt.com/connector_platform_oauth_redirect
https://claude.ai/api/mcp/auth_callback
https://insiders.vscode.dev/redirect
https://oauth.pstmn.io/v1/callback
https://vscode.dev/redirect
http://localhost
http://localhost/callback
http://127.0.0.1
http://127.0.0.1/callback
Contact us if you need additional URIs whitelisted.

Setup Instructions
Add Swiggy MCPs to Cursor
Food	Dineout	Instamart
Install	Install	Install
Add Swiggy MCP Servers to VS Code
Install Food   Install Dineout   Install Instamart

OR
Manual Setup: Update your mcp.json configuration file located in ~/.cursor/mcp.json with:

{
  "mcpServers": {
    "swiggy-instamart": {
      "type": "http",
      "url": "https://mcp.swiggy.com/im"
    },
    "swiggy-food": {
      "type": "http",
      "url": "https://mcp.swiggy.com/food"
    },
    "swiggy-dineout": {
      "type": "http",
      "url": "https://mcp.swiggy.com/dineout"
    }
  }
}
Save the file and reload Cursor to activate the integration.

For Claude Pro Desktop Users
Open Claude Desktop
Go to Settings → Connectors → Add custom connector
Add any Name
Add the URL:
Food: https://mcp.swiggy.com/food
Instamart: https://mcp.swiggy.com/im
Dineout: https://mcp.swiggy.com/dineout
Save and Restart Claude
Other MCP Clients
Add this to your MCP manifest:

{
  "mcpServers": {
    "swiggy-instamart": {
      "type": "http",
      "url": "https://mcp.swiggy.com/im"
    },
    "swiggy-food": {
      "type": "http",
      "url": "https://mcp.swiggy.com/food"
    },
    "swiggy-dineout": {
      "type": "http",
      "url": "https://mcp.swiggy.com/dineout"
    }
  }
}
Workflows You Can Try
Food Ordering Workflows
Cuisine Discovery
Craving something specific, explore options by cuisine

I'm craving some authentic South Indian food. Find me the best-rated restaurants 
nearby that serve dosas and filter coffee. Show me their top dishes.
Late Night Cravings
Finding restaurants open late

It's midnight and I'm hungry. What restaurants are still open near my home? 
I want something quick - maybe biryani or rolls. Show me options with delivery time under 30 mins.
Team Lunch Order
Ordering food for the office team

I need to order lunch for 8 people at the office. We want a mix of:
- Vegetarian options (paneer dishes, dal)
- Non-veg (chicken biryani, kebabs)
- Some healthy salads
Budget is around ₹300 per person. Suggest a restaurant and build the order.
Healthy Meal Finder
Looking for nutritious options

I'm trying to eat healthy. Find me restaurants that serve:
- High protein meals
- Low calorie bowls
- Salads with grilled chicken
Show me options with calorie information if available. Deliver to home.
Budget Meals
Delicious food without breaking the bank

I want a filling dinner under ₹150. Show me the best value meals nearby - 
could be a combo, thali, or biryani. Sort by ratings.
Bolt
Fast delivery during work hours

I have a 30-minute lunch break. Find me restaurants with the fastest delivery 
that serve good North Indian or Chinese food. Order a quick combo meal to office.
Instamart Workflows
Recipe-to-Cart
Planning dinner, need all ingredients in one go.

I'm making Thai Green Curry tonight for 4 people. Can you search for the recipe, 
and let me know what items I need. Use my home address and add everything to cart.
Dietary-Compliant Shopping
Following specific diet (keto, vegan, diabetic, gluten-free)

I'm on a keto diet. Can you help me build a diet chart and the items I need for this. 
Make sure everything is low-carb and add to cart for my home address.
Weekly Meal Prep
Sunday bulk cooking for the work week

I'm doing meal prep for 5 days. I need:
- 1kg chicken breast
- Bulk vegetables (broccoli, bell peppers, sweet potato)
- Quinoa and brown rice
- Olive oil and basic spices
Deliver to my home address.
Party Shopping
Hosting a weekend gathering

I'm hosting a party for 15 people. Help me shop for:
- Party snacks (chips, nachos, popcorn, nuts)
- Beverages (Coke, Sprite, juices)
- Frozen appetizers (paneer tikka, samosas)
- Ice cream and desserts
- Paper plates, cups, napkins
I need delivery to my home address.
Budget Shopping
Student/tight budget, need to stay under limit

I have ₹500 budget for the week. Help me shop smart:
- Instant noodles (Maggi)
- Bread, jam, peanut butter
- Eggs (6-pack)
- Milk (500ml)
- Basic snacks
Show me the cart total and help me stay within budget. Home address.
Brand Comparison
Price-conscious shopping, comparing options

I want to compare brands and prices for my monthly staples:
- Tea (Tata, Brooke Bond, Red Label)
- Cooking oil (Fortune, Sundrop)
- Milk (Amul, Mother Dairy, Nandini)
Show me options and help me pick the best value. Deliver to home.
Dineout Workflows
Weekend Dinner Plans
Finding and booking a table for date night

I want to take my partner out for dinner this Saturday evening around 8 PM. 
Find me good Italian restaurants in Koramangala with romantic ambiance. 
Check availability and book a table for 2 if there are slots.
Group Celebration
Booking for friends or family gatherings

We're celebrating a birthday with 6 friends this Friday at 7:30 PM. 
Find restaurants in Indiranagar that serve North Indian food, have good ratings, 
and can accommodate a group. Check which ones have free booking slots and reserve a table.
Exploring New Restaurants
Discovering dining options with offers

I'm looking to try new restaurants in Whitefield this week. Show me top-rated places 
with active dineout offers. I prefer continental or Asian cuisine. 
What are my options for dinner tomorrow at 8 PM?
Tips for Best Results
For Food & Instamart:

Be Specific - Mention quantities, brands, dietary preferences
Provide Address Early - "Use my home address" or "deliver to office"
Check Cart - Always review cart before checkout
For Dineout:

Specify Locality - Mention area/neighborhood (e.g., "Koramangala", "Indiranagar")
State Party Size - Number of people and date/time
Free Bookings Only - Currently supports free reservations
Important Notes
⚠️ Current Limitations - Third-party app development is not permitted at this time due to ongoing security reviews and compliance requirements. Stay tuned for updates on expanded access.

⚠️ Keep the App Closed - Do not open the Swiggy app while using these MCP integrations. Using both simultaneously may cause session conflicts or order processing issues.

⚠️ COD Orders - Be careful as it can place orders on your behalf under COD. Orders placed cannot be cancelled, so always review your cart before checkout.

Support - For issues or integration questions, contact the Swiggy developer team.

About
No description, website, or topics provided.
Resources
Readme
Activity
Custom properties
Stars
163 stars
Watchers
5 watching
Forks
21 forks
Report repository
Releases
No releases published
Packages
No packages published
Contributors

@aannuuj
@hariom-palkar
@soumyakant2-swiggy
@varunrmantri23
@shivswigs
@varunmantri-swiggy
see how they build what are the edges u can verify from here 

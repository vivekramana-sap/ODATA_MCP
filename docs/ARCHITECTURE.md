# JAM OData MCP Bridge — Architecture Overview

> For architects and technical leads evaluating or integrating this component.

---

## What It Does

The bridge is a **translation layer** between SAP OData services and AI agents. It reads OData `$metadata` at startup, auto-generates a set of strongly-typed MCP tools, and exposes them over HTTP or stdio so any MCP-compatible AI (Claude, Copilot Studio, custom agents) can call SAP APIs in natural language — without the agent needing to know anything about OData.

```
AI Agent (Claude / Copilot Studio)
        │  MCP JSON-RPC
        ▼
┌─────────────────────────────┐
│   JAM OData MCP Bridge       │
│  ┌─────────────────────┐    │
│  │  Bridge (tool gen)  │    │
│  │  Config loader      │    │
│  │  Auth handler       │    │
│  │  Transport layer    │    │
│  └─────────────────────┘    │
└──────────┬──────────────────┘
           │  OData v2 / v4
           ▼
  SAP S/4HANA (on-premise)
  via BTP Connectivity / Cloud Connector
```

---

## Key Design Decisions

### 1. Zero external dependencies
The entire server is pure Python 3.11 stdlib — `urllib`, `http.server`, `json`, `xml.etree`. No `requests`, no `zeep`, no `flask`. This was a deliberate choice for:
- Minimal attack surface
- No `pip install` step in CF build
- Predictable behaviour across environments

### 2. Metadata-driven tool generation
On startup, the bridge fetches `$metadata` from each configured service and auto-generates MCP tools. No manual mapping needed. Adding a new SAP service is a one-line config change:

```json
{ "alias": "my_service", "url": "https://host/sap/opu/odata4/sap/..." }
```

At runtime with the current 4 services configured, this produces **234 MCP tools** covering 37 entity sets and 2 bound actions.

### 3. OData v2 and v4 support
The bridge handles both protocol versions transparently:

| Concern | OData v2 | OData v4 |
|---|---|---|
| Metadata namespace | `schemas.microsoft.com/ado/2008/09/edm` | `docs.oasis-open.org/odata/ns/edm` |
| Response format | `{"d": {"results": [...]}}` | `{"value": [...]}` |
| Capability annotations | Inline `sap:creatable="false"` attributes | Separate `<Annotations Target="...">` blocks |
| DateTime filter syntax | `datetime'2024-01-01T00:00:00'` | `2024-01-01T00:00:00Z` |
| String filter | `substringof('v',F) eq true` | `contains(F,'v')` |
| Count parameter | `$inlinecount=allpages` (collections only) | `$count=true` |

The bridge generates correct filter syntax examples per version so agents produce valid queries.

### 4. Capability-aware tool suppression
The bridge reads OData capability annotations and suppresses tools accordingly — it does not blindly generate CRUD for every entity set:

- `Insertable: false` → no `create` tool generated
- `Updatable: false` → no `update` tool generated
- `Deletable: false` → no `delete` tool generated
- All key fields are `Computed` (server-assigned) → `create` suppressed
- `readonly: true` in config → only filter/count/get/schema tools

This prevents agents from attempting operations the API would reject.

### 5. Service-level access control

Each service entry in `services.json` supports fine-grained access modes:

| Config field | Effect |
|---|---|
| `readonly: true` | Filter, Count, Get, Schema only — no CUD or actions |
| `readonly_but_functions: true` | Filter/Count/Get + Actions — no CUD |
| `include: [...]` | Only expose specified entity sets |
| `include_actions: [...]` | Only expose specified action names |
| `disable_ops: "CUD"` | Runtime flag to suppress create/update/delete |
| `default_top` / `max_top` | Cap result set size per service |

---

## Component Map

```
server.py               — Entry point: argparse, HTTP server loop, health endpoint
services.json           — Service registry (one entry per OData endpoint)
bridge_core/
  config.py             — Loads and validates services.json, initialises ODataService instances
  odata_service.py      — OData client: $metadata parsing, filter/get/create/update/delete/action
  bridge.py             — Tool generator + MCP JSON-RPC dispatcher
  auth.py               — Auth modes: static Basic, passthrough, BTP XSUAA OAuth CC
  transports.py         — stdio transport (Claude Desktop), HTTP handler (BTP CF)
  helpers.py            — OpFilter (operation allow-list per service)
  constants.py          — Operation constants (OP_FILTER, OP_GET, OP_CREATE, ...)
configurator.py         — Local web UI backend for configuring services and credentials
ui/                     — Next.js configurator UI (dev-only, not deployed to CF)
```

---

## Deployment Topology (BTP Cloud Foundry)

```
┌─────────────────────────────────────────────────────────────┐
│  SAP BTP Subaccount                                          │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Cloud Foundry Space                                  │   │
│  │                                                       │   │
│  │  ┌───────────────────────────────────────────┐       │   │
│  │  │  jam-odata-mcp-bridge-v2 (Python app)      │       │   │
│  │  │  256 MB · 1 instance · python_buildpack    │       │   │
│  │  │                                            │       │   │
│  │  │  POST /mcp  ←── AI Agent                  │       │   │
│  │  │  GET  /health                              │       │   │
│  │  └────────────┬──────────────────────────────┘       │   │
│  │               │ bound services                        │   │
│  │ ┌─────────────┼──────────────────────────────┐       │   │
│  │ │  xsuaa      │  connectivity    destination  │       │   │
│  │ │  (JWT auth) │  (proxy)         (config)     │       │   │
│  │ └─────────────┼──────────────────────────────┘       │   │
│  └───────────────┼───────────────────────────────────────┘  │
│                  │ HTTPS via Cloud Connector                  │
└──────────────────┼──────────────────────────────────────────┘
                   │
         ┌─────────▼──────────┐
         │  SAP S/4HANA        │
         │  (on-premise)       │
         │  s4store-dev:44300  │
         └─────────────────────┘
```

**Bound BTP services:**

| Service | Purpose |
|---|---|
| `xsuaa` | JWT-based auth for the MCP HTTP endpoint; validates tokens from Copilot Studio / calling app |
| `connectivity` | Routes HTTP calls to on-premise S/4HANA via Cloud Connector |
| `destination` | Stores named endpoint configs (used for proxy URL resolution) |

---

## Auth Flow

Three supported modes — configurable per deployment:

```
Mode 1: Static credentials (default for dev)
  AI Agent ──── (no auth) ───► MCP Bridge ──── Basic(user,pass) ───► S/4HANA

Mode 2: MCP Basic Auth gate
  AI Agent ──── Basic(mcp_user,mcp_pass) ───► MCP Bridge ──── Basic(svc_creds) ───► S/4HANA

Mode 3: Passthrough (recommended for production)
  AI Agent ──── Bearer(user_token) ───► MCP Bridge ──── Bearer(user_token) ───► S/4HANA
  (each caller authenticates as themselves against SAP — full audit trail)
```

On BTP CF, XSUAA validates the incoming JWT before the request reaches the application.

---

## Currently Configured Services

| Alias | Protocol | System | Entity Sets | Tools | Access Mode |
|---|---|---|---|---|---|
| `ean_weight` | OData v4 | S/4HANA `zsb_jam_weight_ean_reserve` | 2 | 10 | Read + `GenerateEAN_SG` action |
| `business_partner` | OData v2 | S/4HANA `API_BUSINESS_PARTNER` | 2 (filtered) | 14 | Full CRUD |
| `product` | OData v2 | S/4HANA `API_PRODUCT_SRV` | 32 | 202 | Full CRUD |
| `ean_e1` | OData v4 | S/4HANA `zsb_jam_e1_ean_reserve` | 1 | 8 | Read + `GenerateEAN_E1` action |

**Total: 234 MCP tools auto-generated from 37 entity sets across 4 SAP services.**

---

## Request Lifecycle

```
1.  AI agent sends:  POST /mcp  {"method":"tools/call","params":{"name":"product_filter_A_Product","arguments":{"filter":"IndustrySector eq 'NLAG' and CreationDate ge datetime'2026-03-01T00:00:00'","top":10}}}

2.  Transport layer validates auth, parses JSON-RPC

3.  Bridge looks up tool name → (ODataService, operation="filter", target="A_Product")

4.  ODataService builds URL:
      GET /sap/opu/odata/sap/API_PRODUCT_SRV/A_Product
          ?$filter=IndustrySector eq 'NLAG' and CreationDate ge datetime'2026-03-01T00:00:00'
          &$top=10&$format=json&$inlinecount=allpages

5.  Auth handler injects credentials (or forwards caller's token in passthrough mode)

6.  On BTP CF: outbound HTTP goes through Connectivity proxy → Cloud Connector → S/4HANA

7.  Response normalised (v2 "d.results" → v4 "value" shape)

8.  Result returned to agent as JSON-RPC response
```

---

## What Agents Can Do With This

Because each tool is richly described with field names, types, OData-correct filter syntax examples and expansion hints, agents can:

- **Query** — *"Show me all NLAG products created this month"* → builds correct datetime filter
- **Navigate** — *"Fetch sales details of those products"* → uses `expand=to_SalesDelivery` in a single call
- **Act** — *"Generate an E1 EAN for department 10"* → calls the bound SAP action
- **Explore** — *"What fields does A_BusinessPartner have?"* → calls `schema` tool without hitting data
- **Paginate** — *"How many products match? Get page 2"* → uses `count` then `skip+top`

---

## Security Considerations

| Concern | Mitigation |
|---|---|
| Credential storage | `env` file gitignored; `credentials.mtaext` gitignored; env vars in CF |
| MCP endpoint exposure | Protected by XSUAA JWT on BTP; optional Basic Auth for local |
| OData credential leakage | Passthrough mode eliminates server-side credential storage |
| Tool scope creep | `include`, `readonly`, `include_actions` limit tool surface per service |
| SAP system overload | `max_top` cap enforced server-side regardless of what agent requests |
| Injection | All filter/key values pass through `urllib.parse.quote`; no string interpolation into SQL |

---

## Extensibility

| To add... | Change needed |
|---|---|
| New SAP OData service | Add one entry to `services.json`; restart |
| New entity set from existing service | Remove or adjust `include` list in `services.json` |
| Restrict operations on a service | Set `readonly`, `readonly_but_functions`, or `disable_ops` |
| Expose a new SAP function/action | Add to `include_actions` list |
| New AI client | Any MCP-compatible client works — the protocol is standard |
| New auth mode | Extend `bridge_core/auth.py` |

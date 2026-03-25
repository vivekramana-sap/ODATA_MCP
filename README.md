# JAM OData MCP Bridge

A lightweight, zero-dependency Python server that bridges SAP OData v4 services to the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It auto-discovers entity sets and actions from `$metadata` and exposes them as typed MCP tools — ready to use with Copilot Studio, Claude, or any MCP-compatible AI client.

---

## Features

- **Auto-discovery** — reads `$metadata` on startup to generate tools for every entity set and action
- **Multi-service** — configure multiple OData endpoints, each namespaced by alias
- **Full CRUD** — `filter`, `count`, `get`, `create`, `update` (PATCH), `delete`, and bound/unbound `action` tools
- **Auth modes** — static service credentials, Basic Auth gate, or passthrough (caller credentials forwarded to OData)
- **BTP on-premise** — automatic Connectivity service proxy for SAP Cloud Foundry
- **CORS-enabled** — works with browser-based MCP clients and Copilot Studio
- **No external dependencies** — pure Python 3.11 stdlib

---

## Quick Start (Local)

```bash
# 1. Edit services.json with your OData endpoint(s)
# 2. Set credentials (if using env-var placeholders in services.json)
export ODATA_USERNAME=your-user
export ODATA_PASSWORD=your-password

# 3. Run
python3 server.py --config services.json --port 7777
```

Health check: `GET http://localhost:7777/health`
MCP endpoint: `POST http://localhost:7777/mcp`

---

## Configuration

### `services.json`

Each entry defines one OData service:

```json
[
  {
    "alias": "my_service",
    "url": "https://host/sap/opu/odata4/sap/my_service/srvd/.../0001/",
    "username": "${ODATA_USERNAME}",
    "password": "${ODATA_PASSWORD}",
    "passthrough": false
  }
]
```

| Field | Description |
|---|---|
| `alias` | Prefix for all generated tool names (e.g. `my_service_filter_Orders`) |
| `url` | OData v4 service root URL |
| `username` / `password` | Service credentials. Supports `${ENV_VAR}` placeholders |
| `passthrough` | If `true`, forwards the caller's `Authorization` header to OData instead of using service credentials |

### CLI Options / Environment Variables

| Argument | Env Var | Default | Description |
|---|---|---|---|
| `--config` | `CONFIG_FILE` | `services.json` | Path to services config |
| `--port` | `PORT` | `7777` | Listening port |
| `--username` | `MCP_USERNAME` | _(none)_ | MCP Basic Auth username |
| `--password` | `MCP_PASSWORD` | _(none)_ | MCP Basic Auth password |
| `--passthrough` | `MCP_PASSTHROUGH` | `false` | Forward caller credentials to OData |

---

## Generated MCP Tools

For each entity set `<ES>` in service `<alias>`, these tools are generated:

| Tool | Description |
|---|---|
| `<alias>_filter_<ES>` | List/filter with `$filter`, `$select`, `$orderby`, `$expand`, `$top`, `$skip`, `$count` |
| `<alias>_count_<ES>` | Count entities matching an optional filter |
| `<alias>_get_<ES>` | Fetch a single entity by key |
| `<alias>_create_<ES>` | Create a new entity |
| `<alias>_update_<ES>` | Update an entity (PATCH) |
| `<alias>_delete_<ES>` | Delete an entity |
| `<alias>_action_<Name>` | Invoke a bound or unbound OData action |

---

## Auth Modes

### No auth (open)
Don't set `MCP_USERNAME` — all requests to `/mcp` are accepted.

### Static MCP credentials
```bash
python3 server.py --username admin --password secret
```
Clients must send `Authorization: Basic <base64>`.

### Passthrough (end-user auth)
```bash
python3 server.py --passthrough
```
The server requires any `Authorization` header and forwards it verbatim to the OData service. Each MCP caller authenticates as themselves against SAP.

---

## Deploy to SAP BTP Cloud Foundry

### Prerequisites

- [`cf` CLI](https://docs.cloudfoundry.org/cf-cli/) logged in to your BTP subaccount
- [`mbt`](https://sap.github.io/cloud-mta-build-tool/) (MTA build tool)
- Connectivity and Destination service instances available

### Steps

```bash
# 1. Fill in credentials
cp credentials.mtaext.template credentials.mtaext
# Edit credentials.mtaext and set ODATA_USERNAME / ODATA_PASSWORD

# 2. Deploy
bash deploy.sh
```

`deploy.sh` will:
1. Verify CF login
2. Build the MTA archive (`mbt build`)
3. Deploy with `cf deploy` using the credentials extension

The app will be available at `https://<route>/mcp`.

### BTP Connectivity (On-Premise)

When deployed to CF, the server automatically reads `VCAP_SERVICES` to configure the Connectivity service proxy. On-premise OData systems reachable via the SAP Cloud Connector are accessed transparently.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` or `/health` | Health check — returns service list and tool count |
| `POST` | `/mcp` | MCP JSON-RPC endpoint |
| `OPTIONS` | `/mcp` | CORS preflight |

### MCP Methods Supported

- `initialize` — returns server capabilities
- `tools/list` — returns all generated tool schemas
- `tools/call` — invokes a tool

---

## Project Structure

```
├── server.py                      # Main server (single file, no dependencies)
├── services.json                  # OData service configuration
├── runtime.txt                    # Python version for CF buildpack
├── manifest.yml                   # CF push manifest
├── mta.yaml                       # MTA deployment descriptor
├── deploy.sh                      # One-command BTP CF deployment script
├── credentials.mtaext.template    # Template for deployment credentials
└── credentials.mtaext             # Your credentials (gitignored — never commit)
```

---

## Security Notes

- `credentials.mtaext` is gitignored — never commit it
- For production, prefer `--passthrough` mode so credentials are not stored server-side
- The MCP endpoint should always be protected with `--username`/`--password` or network-level access control when exposed publicly

# Python v2 vs Go — OData MCP Bridge Comparison

Comprehensive feature comparison between the **Python v2** (`jam-odata-mcp-bridge 2`) and **Go** (`odata_mcp_go`) implementations of the OData MCP Bridge, covering both OData v2 and v4 protocol support.

---

## Code Size & Architecture

| Dimension | Python v2 | Go |
|---|---|---|
| Total LoC (non-test) | **2,540** | **7,545** |
| External dependencies | **0** (stdlib only) | Go std + `mapstructure` |
| Package structure | 8 modules in `bridge_core/` | 12 packages in `internal/` |
| Entry point | `server.py` (331 lines) | `cmd/odata-mcp/main.go` (612 lines) |
| Build artefact | Source files (no build step) | Single static binary |

### Module Breakdown

| Concern | Python v2 | Go |
|---|---|---|
| Constants / types | `constants.py` (42) | `constants/` (312) + `models/` (181) |
| Helpers / utils | `helpers.py` (213) | `utils/date.go` (217) + `utils/numeric.go` (160) + `client/formatting.go` (77) |
| Authentication | `auth.py` (184) | Built into `client/http.go` (243) |
| Metadata parsing | Inside `odata_service.py` (~200) | `metadata/parser.go` (280) + `parser_v4.go` (504) |
| Tool generation | `bridge.py` (652) | `bridge/generators.go` (456) + `bridge/handlers.go` (412) |
| Transport | `transports.py` (315) | `transport/` (893) — stdio, SSE, streamable HTTP |
| OData HTTP client | Inside `odata_service.py` (~300) | `client/` (596) |
| Config loader | `config.py` (62) | `config/config.go` (135) + `main.go` flags |
| Hint system | — | `hint/hint.go` (384) |

---

## OData v2 Support

| Feature | Python v2 | Go | Notes |
|---|---|---|---|
| **Auto-detect v2 from $metadata** | ✅ Multiple EDM NS fallback (2006/2007/2008) | ✅ Separate `parser.go` for v2 | Both detect automatically |
| **SAP `sap:` attributes** | ✅ `sap:creatable`, `sap:updatable`, `sap:deletable`, `sap:searchable`, `sap:pageable` | ✅ Same attributes via XML struct tags | Parity |
| **SAP property labels** | ✅ `sap:label` extracted and included in tool descriptions | ❌ Not extracted | Python v2 provides richer tool descriptions |
| **FunctionImport (GET)** | ✅ Parses `m:HttpMethod`, builds query params with OData quoting (`'string'`) | ✅ Parses HTTP method, formatted params | Parity |
| **FunctionImport (POST)** | ✅ CSRF token + JSON body | ✅ CSRF token + JSON body | Parity |
| **CSRF token management** | ✅ Fetch from `$metadata` before each write | ✅ Fetch before each write, retry-on-403 | Go has retry logic |
| **Legacy date `/Date(ms)/`** | ✅ Regex-based conversion to ISO-8601 | ✅ Regex-based conversion | Parity |
| **$inlinecount** | ❌ Uses `$count` everywhere (v4 syntax) | ✅ Auto-adds `$inlinecount=allpages` for v2 | Go handles v2 count natively |
| **$format=json** | ❌ Not added for v2 | ✅ Auto-adds `$format=json` for v2 | Go ensures JSON response from v2 services |
| **v2 $filter syntax** | ✅ Passes through (user must know v2 syntax) | ✅ Passes through | Parity |
| **Navigation properties (v2)** | ✅ Extracts `Relationship`, `ToRole`, `FromRole` | ✅ Extracts navigation info | Parity |
| **Update method (MERGE)** | ✅ `_method` param: `PATCH` / `MERGE` / `PUT` | ✅ `_method` param: `PUT` / `PATCH` / `MERGE` | Parity — both support SAP's MERGE |
| **SAP GUID formatting** | ❌ Not auto-wrapped | ✅ Auto-wraps `guid'...'` in filters and key predicates | Go handles SAP's non-standard GUID quoting |

### OData v2 Verdict

**Go leads** on v2 protocol correctness — it auto-adds `$format=json`, `$inlinecount=allpages`, and wraps GUIDs with `guid'...'`. Python v2 compensates with richer tool descriptions (SAP labels) but relies on the OData service to default to JSON and the LLM to use v4-style query syntax.

---

## OData v4 Support

| Feature | Python v2 | Go | Notes |
|---|---|---|---|
| **Auto-detect v4 from $metadata** | ✅ Checks for OASIS EDM namespace | ✅ `IsODataV4()` detection | Parity |
| **EntityType parsing** | ✅ Keys, properties, nullable, labels | ✅ Keys, properties, nullable, base type, abstract, open type | Go has more complete type model |
| **ComplexType parsing** | ❌ Not parsed | ✅ Parsed with full property model | Go handles complex types |
| **EnumType parsing** | ❌ Not parsed | ✅ Parsed with members and values | Go handles enums |
| **NavigationProperty (v4)** | ✅ Type, Partner, Nullable | ✅ Type, Partner, Nullable, ContainsTarget | Go captures containment nav |
| **Capability annotations** | ✅ SearchRestrictions, InsertRestrictions, UpdateRestrictions, DeleteRestrictions | ✅ SearchRestrictions | Python v2 parses more capability annotations |
| **Bound Actions** | ✅ Parses IsBound, binding parameter, entity set resolution | ❌ Actions treated as imports only | Python v2 has fuller action model |
| **Bound Functions** | ✅ Skips binding parameter, exposes remaining params | ❌ Functions treated as imports only | Python v2 supports bound functions |
| **Action/Function Imports** | ✅ ActionImport + FunctionImport parsed | ✅ ActionImport + FunctionImport parsed | Parity |
| **Unbound Actions** | ✅ Full support with namespace-qualified URL | ❌ Only via imports | Python v2 handles the `Namespace.ActionName` URL pattern |
| **Entity key as individual params** | ✅ Each key is a separate tool param with type hints | ✅ Each key is a separate tool param | Parity |
| **$count** | ✅ `$count=true` in filter, dedicated count tool | ✅ Translates to v4 `$count=true` | Parity |
| **$search** | ✅ Separate search tool when searchable | ✅ Separate search tool when searchable | Parity |
| **$expand** | ✅ nav properties listed in description | ✅ Supported as parameter | Parity |
| **$select** | ✅ Field list in description | ✅ Supported as parameter | Parity |
| **OData-Version header** | ✅ Sends `OData-Version: 4.0` on all requests | ✅ Sets `Accept: application/json;odata.metadata=minimal` for v4 | Different approach, both work |
| **Entity inheritance (BaseType)** | ❌ Not handled | ✅ Parsed (not resolved across types) | Go captures it in metadata |

### OData v4 Verdict

**Python v2 leads** on v4 action/function support — it correctly handles bound actions (with entity key resolution), unbound actions with `Namespace.ActionName` URLs, and parses more capability annotations (Insert/Update/Delete restrictions). Go has a more complete type system model (complex types, enums, base types) but doesn't leverage them in tool generation.

---

## MCP Protocol & Tool Generation

| Feature | Python v2 | Go | Notes |
|---|---|---|---|
| **Protocol version** | `2024-11-05` | Configurable via `--protocol-version` | Go has AI Foundry compat |
| **Tool naming** | `{alias}_{op}_{EntitySet}` | Configurable: prefix/postfix, shrink mode | Go more flexible |
| **Tool descriptions** | Rich: field lists, key hints, SAP-aware examples, filter syntax help, OData operators reference | Basic: `"List/filter X entities with OData query options"` | **Python v2 significantly richer** |
| **Property type hints** | ✅ Per-property: date format, GUID format, decimal advice, integer range, nullable notes | ❌ Only JSON Schema type | Python v2 gives LLMs much better guidance |
| **Key predicate examples** | ✅ SAP-aware: Plant=1000, Material='MAT-001', GUID without quotes | ❌ None | Python v2 generates realistic examples |
| **$filter description** | ✅ Type-aware examples: `contains(Name,'ABC')`, `Date ge 2024-01-01` | ❌ Just `"OData filter expression"` | Python v2 teaches the LLM OData syntax |
| **Schema discovery tool** | ✅ Per-entity-set `schema_X` tool | ❌ Only service-level info tool | Python v2 lets LLM discover field details per entity |
| **Service info tool** | ✅ Returns entity sets, actions, auth type, limits | ✅ Returns entity sets, types, functions, hints | Parity |
| **`additionalProperties: false`** | ✅ Set on all tool schemas | ❌ Not set | Python v2 prevents hallucinated parameters |
| **Required fields** | ✅ Non-nullable non-key fields marked required on create | ✅ Non-nullable fields marked required | Parity |
| **Universal tool mode** | ❌ Not implemented | ✅ Single tool for all operations | Go unique feature |
| **Hint system** | ❌ Not implemented | ✅ JSON hint files + CLI `--hint` | Go unique feature |

---

## Transport & Networking

| Feature | Python v2 | Go |
|---|---|---|
| **stdio** | ✅ Line-delimited JSON-RPC | ✅ Line-delimited JSON-RPC |
| **HTTP (JSON-RPC)** | ✅ ThreadingHTTPServer | ✅ Streamable HTTP |
| **SSE (Server-Sent Events)** | ❌ | ✅ Full SSE transport |
| **Streamable HTTP** | ❌ | ✅ Modern MCP transport |
| **CORS** | ✅ Full CORS headers | ✅ Via streamable transport |
| **Health endpoint** | ✅ `/health`, `/healthz` | ❌ (not in transport layer) |
| **Localhost guard** | ✅ `--i-am-security-expert` for non-localhost | ✅ Security middleware |

---

## Authentication & Security

| Feature | Python v2 | Go |
|---|---|---|
| **Basic auth to OData** | ✅ | ✅ |
| **Cookie auth** | ✅ Netscape cookie file + cookie string | ✅ Cookie file + string |
| **Passthrough auth** | ✅ Forward caller Authorization header | ✅ Forward MCP headers to OData |
| **MCP Bearer token** | ✅ `--mcp-token` / `--mcp-token-file` | ❌ (security module exists but different) |
| **BTP Connectivity proxy** | ✅ Full VCAP_SERVICES integration, JWT refresh | ❌ |
| **XSUAA / OAuth 2.0** | ✅ Token introspection, authorize endpoint, dynamic client registration | ❌ |
| **OAuth metadata well-known** | ✅ `/.well-known/oauth-authorization-server` | ❌ |
| **Input guards** | ✅ String length caps in `_guard_params()` | ❌ |
| **Max response size** | ✅ `--max-response-size` with structured error | ✅ `--max-response-size` |
| **Max items** | ✅ `--max-items` + pagination hint | ✅ `--max-items` |

---

## Configuration & Operations

| Feature | Python v2 | Go |
|---|---|---|
| **Multi-service** | ✅ services.json with multiple entries | ❌ Single service URL |
| **$env{VAR} in config** | ✅ `${VAR}` expansion in services.json | Related: env for single-service flags |
| **Entity filtering** | ✅ fnmatch wildcards: `Product*`, `Order*` | ✅ Prefix wildcards: `Product*` |
| **Action/function filtering** | ✅ `include_actions` list | ✅ `--functions` flag |
| **Op filtering** | ✅ `--enable` / `--disable` (CSFGUDA/R) | ✅ `--enable-ops` / `--disable-ops` |
| **Read-only mode** | ✅ `--read-only`, `--read-only-but-functions` | ✅ `--read-only`, `--read-only-but-functions` |
| **Legacy date conversion** | ✅ On by default, `--no-legacy-dates` to disable | ✅ `--legacy-dates` to enable |
| **Claude Code friendly** | ✅ `-c` strips `$` from param names | ✅ `--claude-code-friendly` |
| **Verbose errors** | ✅ `--verbose-errors` | ✅ `--verbose-errors` |
| **Trace / debug** | ✅ `--trace` dumps tools and exits | ✅ `--trace` with file output |
| **Pagination hints** | ✅ Automatic in filter responses | ✅ `--pagination-hints` flag |
| **Sort tools** | ✅ `--sort-tools` / `--no-sort-tools` | ✅ `--sort-tools` |
| **Default top** | ✅ Per-service configurable (default 50) | ❌ Not configurable |
| **Max top** | ✅ Per-service cap (default 500) | ❌ Not capped |
| **Graceful shutdown** | ✅ SIGTERM/SIGINT handler | ✅ Context cancellation |

---

## SAP-Specific Features

| Feature | Python v2 | Go |
|---|---|---|
| **SAP property labels** | ✅ Displayed in tool descriptions | ❌ |
| **SAP GUID handling** | ❌ No auto `guid'...'` wrapping | ✅ Auto-wraps in filters and keys |
| **SAP date `/Date(ms)/`** | ✅ Auto-converts to ISO-8601 | ✅ Auto-converts |
| **SAP capability attrs** | ✅ `sap:creatable`, etc. | ✅ Same attributes |
| **SAP namespace detection** | ✅ EDM NS fallback chain | ✅ Similar detection |
| **SAP service detection** | ❌ No explicit detection | ✅ URL + metadata heuristics |
| **SAP numeric handling** | ❌ | ✅ `utils/numeric.go` — string-to-number conversion |
| **BTP Cloud Foundry** | ✅ Full BTP deployment (manifest.yml, mta.yaml, XSUAA) | ❌ Binary deployment only |

---

## Summary Scorecard

| Category | Python v2 | Go | Winner |
|---|---|---|---|
| **OData v2 protocol** | Good | Better | Go (auto `$format`, `$inlinecount`, GUID) |
| **OData v4 protocol** | Better | Good | Python v2 (bound actions, capability annotations) |
| **Tool descriptions** | Excellent | Basic | **Python v2** (rich type hints, examples, syntax) |
| **LLM guidance quality** | Excellent | Basic | **Python v2** (schema tools, key examples, filter help) |
| **Transports** | 2 (stdio, HTTP) | 3 (stdio, SSE, streamable) | Go |
| **SAP Integration** | Better | Good | Python v2 (BTP proxy, XSUAA, labels) |
| **Authentication** | More options | Fewer options | Python v2 (BTP, XSUAA, OAuth) |
| **Multi-service** | ✅ Native | ❌ Single only | **Python v2** |
| **Performance** | Adequate (threading) | Superior (compiled, goroutines) | Go |
| **Deployment** | Source + runtime | Single binary | Go |
| **Zero dependencies** | ✅ | ❌ | Python v2 |
| **Code size** | 2,540 LoC | 7,545 LoC | Python v2 (3× smaller) |

### Bottom Line

**Python v2 excels at LLM interaction quality** — the rich tool descriptions, type-aware hints, per-property guidance, and schema discovery tools mean LLMs make fewer mistakes when generating OData queries. Combined with multi-service support and full BTP/XSUAA authentication, it is the better choice for SAP BTP Cloud Foundry deployments and complex multi-service scenarios.

**Go excels at runtime characteristics** — compiled binary, modern MCP transports (SSE, streamable HTTP), SAP GUID auto-formatting, and proper v2 protocol handling (`$format`, `$inlinecount`). It is the better choice for standalone local deployment and services that require streaming transports.

For SAP customers, **Python v2 currently provides better end-to-end value** due to its significantly richer tool descriptions (which directly improve LLM accuracy) and native BTP deployment support. The Go bridge's v2 protocol correctness advantages (`$format=json`, `$inlinecount`, GUID wrapping) are worth backporting to Python v2.

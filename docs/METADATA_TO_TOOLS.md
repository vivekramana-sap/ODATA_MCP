# How OData Metadata Becomes MCP Tools

This document walks through the complete pipeline using the real
`API_BUSINESS_PARTNER` service as a running example.

---

## Overview

```
SAP $metadata XML
      │
      ▼  Step 1 — HTTP fetch
Raw XML (EntityTypes, EntitySets, Actions, Annotations)
      │
      ▼  Step 2 — Parse: odata_service._load_metadata()
Python dict  {entity_sets, actions, capabilities}
      │
      ▼  Step 3 — Generate: bridge._gen_tools()
MCP tool list  [{name, description, inputSchema}, ...]
      │
      ▼  Step 4 — Dispatch: bridge.handle()
OData HTTP request  GET /EntitySet?$filter=...
      │
      ▼  Step 5 — Normalise & Return
Tool result text → LLM
```

---

## Step 1 — Fetch `$metadata`

On startup, for the `business partner` service the bridge calls:

```
GET http://s4store-dev.jumbo.local:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/$metadata
Authorization: Basic bmFnYXJhanY6...
```

The SAP server returns a large EDMX XML document.  The relevant sections
that the bridge extracts are shown below.

### 1a — EntityType  (defines fields + keys)

```xml
<EntityType Name="A_BusinessPartnerType">

  <!-- The primary key -->
  <Key>
    <PropertyRef Name="BusinessPartner"/>
  </Key>

  <!-- Properties — each carries type, nullability, and SAP labels/annotations -->
  <Property Name="BusinessPartner"          Type="Edm.String"  Nullable="false"
            sap:label="Zakenpartner"/>

  <Property Name="Customer"                 Type="Edm.String"  Nullable="true"
            sap:label="Klant"
            sap:creatable="false"  sap:updatable="false"/>   ← read-only field

  <Property Name="Supplier"                 Type="Edm.String"  Nullable="true"
            sap:label="Leverancier"
            sap:creatable="false"  sap:updatable="false"/>   ← read-only field

  <Property Name="AcademicTitle"            Type="Edm.String"  Nullable="true"
            sap:label="Academische titel 1"/>

  <Property Name="BusinessPartnerCategory"  Type="Edm.String"  Nullable="true"
            sap:label="Type zakenpartner"/>

  <Property Name="BusinessPartnerFullName"  Type="Edm.String"  Nullable="true"
            sap:creatable="false"  sap:updatable="false"/>   ← computed / read-only

  <!-- Navigation properties → links to related entity sets -->
  <NavigationProperty Name="to_BPCreditWorthiness"  .../>
  <NavigationProperty Name="to_BusinessPartnerAddress" .../>
  <NavigationProperty Name="to_Customer"            .../>
  ...
</EntityType>
```

### 1b — EntitySet  (controls CRUD capabilities)

```xml
<EntitySet Name="A_BusinessPartner"
           EntityType="API_BUSINESS_PARTNER.A_BusinessPartnerType"
           sap:deletable="false"     ← DELETE will be suppressed
           sap:searchable="false"/>  ← no full-text search tool
```

> **Why no `sap:creatable` / `sap:updatable` here?**
> When those attributes are absent the bridge defaults them to `true` —
> SAP omits the attribute when the default applies.

### 1c — External Annotations  (OData v4 Capabilities, even in v2 services)

SAP always places capability restrictions in a separate `<Annotations>`
block using the OData v4 namespace — even in v2 (ADO-namespace) metadata:

```xml
<Annotations Target="API_BUSINESS_PARTNER.API_BUSINESS_PARTNER_Entities/A_BusinessPartner"
             xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <Annotation Term="Capabilities.NavigationRestrictions">
    <Record>
      <PropertyValue Property="RestrictedProperties">
        <Collection>
          <Record>
            <PropertyValue Property="NavigationProperty"
                           NavigationPropertyPath="to_BuPaIdentification"/>
            <PropertyValue Property="ReadRestrictions">
              <Record>
                <PropertyValue Property="Description"
                  String="Identificatiegegevens zakenpartner ophalen"/>
              </Record>
            </PropertyValue>
          </Record>
        </Collection>
      </PropertyValue>
    </Record>
  </Annotation>
</Annotations>
```

These are processed by `_apply_external_annotations()` to pick up any
insert/update/delete restrictions that override the EntitySet-level caps.

---

## Step 2 — Parse into Python dicts  (`_load_metadata`)

**Code location:** [`bridge_core/odata_service.py`](../bridge_core/odata_service.py)

The XML is parsed by `xml.etree.ElementTree` into two structures:

### 2a — `entity_types` dict  (intermediate, keyed by type name)

```python
entity_types["A_BusinessPartnerType"] = {
    "keys": ["BusinessPartner"],
    "props": {
        "BusinessPartner": {
            "type":     "string",       # JSON Schema type (from EDM→JSON map)
            "edm_type": "Edm.String",   # original EDM type kept for filter hints
            "nullable": False,          # Nullable="false" → required on create
            "label":    "Zakenpartner", # sap:label → used in tool description
            "internal": False,
        },
        "Customer": {
            "type":          "string",
            "edm_type":      "Edm.String",
            "nullable":      True,
            "label":         "Klant",
            "sap_creatable": False,     # sap:creatable="false" → excluded from create body
            "sap_updatable": False,     # sap:updatable="false" → excluded from update body
        },
        "AcademicTitle": {
            "type":     "string",
            "edm_type": "Edm.String",
            "nullable": True,
            "label":    "Academische titel 1",
        },
        "BusinessPartnerCategory": {
            "type":     "string",
            "edm_type": "Edm.String",
            "nullable": True,
            "label":    "Type zakenpartner",
        },
        # ... ~115 more properties
    },
    "nav_props": [
        "to_BPCreditWorthiness",
        "to_BusinessPartnerAddress",
        "to_Customer",
        "to_Supplier",
        # ...
    ],
}
```

### 2b — `entity_sets` dict  (final, keyed by set name, merges in capabilities)

The EntitySet element's SAP capability attributes are merged in:

```python
self.entity_sets["A_BusinessPartner"] = {
    # --- copied from entity_types["A_BusinessPartnerType"] ---
    "keys":  ["BusinessPartner"],
    "props": { ... same as above ... },
    "nav_props": ["to_BPCreditWorthiness", "to_BusinessPartnerAddress", ...],

    # --- capability flags from EntitySet element ---
    "capabilities": {
        "creatable":  True,    # sap:creatable absent → defaults True
        "updatable":  True,    # sap:updatable absent → defaults True
        "deletable":  False,   # sap:deletable="false" → DELETE tool suppressed
        "searchable": False,   # sap:searchable absent → defaults False
        "pageable":   True,    # sap:pageable absent   → defaults True
    }
}
```

**EDM → JSON type mapping** (from `bridge_core/constants.py`):

| EDM type           | JSON Schema type | Notes                            |
|--------------------|-----------------|----------------------------------|
| `Edm.String`       | `string`        |                                  |
| `Edm.Int32`        | `integer`       |                                  |
| `Edm.Int64`        | `integer`       |                                  |
| `Edm.Decimal`      | `string`        | string to preserve SAP precision |
| `Edm.Boolean`      | `boolean`       |                                  |
| `Edm.DateTime`     | `string`        | + format hint in description     |
| `Edm.DateTimeOffset` | `string`      | + format hint in description     |
| `Edm.Date`         | `string`        | + format hint in description     |
| `Edm.Guid`         | `string`        | + uuid format                    |
| `Edm.Binary`       | `string`        | base64url hint                   |

---

## Step 3 — Generate MCP tools  (`_gen_tools`)

**Code location:** [`bridge_core/bridge.py`](../bridge_core/bridge.py)

For each entity set the bridge generates up to **8 tools**.  The alias
`business partner` is sanitised to `business_partner` for use in names.

```
A_BusinessPartner (capabilities: create=✓ update=✓ delete=✗ search=✗)
  │
  ├─ business_partner__info                         ← always generated (1 per service)
  ├─ business_partner_schema_A_BusinessPartner      ← describe fields
  ├─ business_partner_filter_A_BusinessPartner      ← list / search
  ├─ business_partner_count_A_BusinessPartner       ← count rows
  ├─ business_partner_get_A_BusinessPartner         ← fetch by key
  ├─ business_partner_create_A_BusinessPartner      ← create (caps.creatable=True)
  ├─ business_partner_update_A_BusinessPartner      ← update (caps.updatable=True)
  └─ (NO delete tool)                               ← caps.deletable=False → suppressed
     (NO search tool)                               ← caps.searchable=False → suppressed
```

### 3a — `filter` tool  (most used)

The `filter` tool lets the LLM query with `$filter`, `$top`, `$select`, etc.

```json
{
  "name": "business_partner_filter_A_BusinessPartner",
  "description": "[business_partner] Search/list/lookup A_BusinessPartner — use for any open-ended request or when you don't have an exact key value yet. Returns up to 2 records by default (server max: 2). Key fields: BusinessPartner. To fetch related data in one call use expand= with: to_BPCreditWorthiness, to_BusinessPartnerAddress, to_Customer, ... Use skip+top for pagination.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "filter": {
        "type": "string",
        "description": "OData $filter expression. Operators: eq, ne, lt, le, gt, ge. Logic: and, or, not. String: substringof('v',F) eq true, startswith(F,'v'). DateTime: datetime'2024-01-01T00:00:00'. Null: F eq null. Example: substringof('ABC',BusinessPartner) eq true and Customer eq 'VALUE'. Fields: BusinessPartner, Customer, Supplier, AcademicTitle, ..."
      },
      "select": {
        "type": "string",
        "description": "Comma-separated fields to return. Reduces payload size. Available: BusinessPartner, Customer, Supplier, AcademicTitle, ..."
      },
      "orderby": {
        "type": "string",
        "description": "Sort expression. Example: BusinessPartner desc. Multiple: Field1 asc, Field2 desc."
      },
      "expand": {
        "type": "string",
        "description": "Comma-separated navigation properties to inline. Available: to_BPCreditWorthiness, to_BusinessPartnerAddress, to_Customer, ..."
      },
      "top": {
        "type": "integer",
        "description": "Max records to return. Default: 2. Max: 2.",
        "minimum": 1,
        "maximum": 2
      },
      "skip": {
        "type": "integer",
        "description": "Records to skip for pagination.",
        "minimum": 0
      },
      "count": {
        "type": "boolean",
        "description": "Set true to include @odata.count in the response."
      }
    }
  }
}
```

**Key decisions made during generation:**

| Source in metadata | Code decision | Effect in tool |
|--------------------|---------------|----------------|
| `max_top=2` from services.json | `"maximum": 2` on `top` param | LLM cannot request more than 2 rows |
| `sap:searchable` absent (defaults False) | No `search` param added to filter | LLM cannot use `$search` |
| Nav props exist | `expand` param lists them with description | LLM knows what to expand |
| `odata_version="2"` | `substringof('v',F) eq true` in filter hint | Correct v2 syntax shown to LLM |

### 3b — `get` tool  (single record by key)

Only the key field (`BusinessPartner`) becomes a required parameter:

```json
{
  "name": "business_partner_get_A_BusinessPartner",
  "description": "[business_partner] Fetch one A_BusinessPartner record by its EXACT key — ONLY use this when you already have the precise key value(s) from a previous filter/search result. Requires ALL key field(s): BusinessPartner. Example: BusinessPartner='VALUE'.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["BusinessPartner"],
    "properties": {
      "BusinessPartner": {
        "type": "string",
        "description": "Zakenpartner. Required — server rejects null."
        // ↑ label "Zakenpartner" came from sap:label in metadata
        // ↑ "Required" because Nullable="false" and is_key=True
      },
      "select": { "type": "string", "description": "Fields to return. Available: ..." },
      "expand": { "type": "string", "description": "Navigation properties to expand. Available: ..." }
    }
  }
}
```

### 3c — `create` tool  (property filtering in action)

Only properties where `sap:creatable` is not `"false"` appear.  This
means `Customer`, `Supplier`, and `BusinessPartnerFullName` are **excluded**
because SAP marked them `sap:creatable="false"`:

```json
{
  "name": "business_partner_create_A_BusinessPartner",
  "description": "[business_partner] Create a new A_BusinessPartner record. Key fields (BusinessPartner) are optional — omit for server-generated keys.",
  "inputSchema": {
    "properties": {
      "AcademicTitle":           { "type": "string", "description": "Academische titel 1." },
      "BusinessPartnerCategory": { "type": "string", "description": "Type zakenpartner." },
      // ← "Customer" NOT here:          sap:creatable="false"
      // ← "Supplier" NOT here:          sap:creatable="false"
      // ← "BusinessPartnerFullName" NOT: sap:creatable="false"
      ...
    }
  }
}
```

### 3d — No `delete` tool

Because `sap:deletable="false"` on the EntitySet:

```python
# bridge.py _gen_tools() — the guard:
if svc.op_filter.allows(OP_DELETE) and caps.get("deletable", True):
    tools.append(...)       # ← never entered: caps["deletable"] == False
```

The delete tool simply does not appear in the tool list —the LLM has no
way to trigger a DELETE even if it wanted to.

---

## Step 4 — Dispatch: tool call → OData URL

**Code location:** [`bridge_core/bridge.py handle()`](../bridge_core/bridge.py) →
[`bridge_core/odata_service.py filter()/get()/create()/...`](../bridge_core/odata_service.py)

When the LLM calls a tool, the bridge looks it up in `_tool_map`:

```
bridge.handle({
  "method": "tools/call",
  "params": {
    "name":      "business_partner_filter_A_BusinessPartner",
    "arguments": { "filter": "substringof('Jumbo',BusinessPartner) eq true", "top": 2 }
  }
})
```

1. `_tool_map["business_partner_filter_A_BusinessPartner"]` → `(svc, "filter", "A_BusinessPartner")`
2. Routes to `svc.filter("A_BusinessPartner", args)`
3. Assembles URL:

```
GET http://s4store-dev.jumbo.local:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner
    ?$format=json
    &$filter=substringof('Jumbo',BusinessPartner) eq true
    &$top=2
```

For a `get` call like `{"BusinessPartner": "0010000001"}`:

```
GET .../A_BusinessPartner('0010000001')
    ?$format=json
```

For a `create` call:

```
POST .../A_BusinessPartner
Content-Type: application/json
x-csrf-token: <fetched from SAP first>

{ "AcademicTitle": "Dr.", "BusinessPartnerCategory": "1" }
```

---

## Step 5 — Normalise and return

**Code location:** [`bridge_core/odata_service.py _normalize_v2_response()`](../bridge_core/odata_service.py)

The SAP v2 response wraps data in `d.results`:

```json
{
  "d": {
    "results": [
      {
        "__metadata": { "id": "...", "type": "API_BUSINESS_PARTNER.A_BusinessPartnerType" },
        "BusinessPartner": "0010000001",
        "CreationDate": "/Date(1609459200000)/",
        "Customer": "C-001"
      }
    ]
  }
}
```

The bridge normalises this to a clean v4-style response:

```json
{
  "value": [
    {
      "BusinessPartner": "0010000001",
      "CreationDate": "2021-01-01T00:00:00Z",
      "Customer": "C-001"
    }
  ]
}
```

**Transformations applied:**

| Raw SAP value          | Normalised value              | Reason                           |
|------------------------|-------------------------------|----------------------------------|
| `d.results[...]`       | `value[...]`                  | OData v2 → v4 shape              |
| `/Date(1609459200000)/`| `2021-01-01T00:00:00Z`        | `legacy_dates=true` (default)    |
| `__metadata` key       | removed                       | Internal SAP field               |
| `SAP__*` / `__*` props | removed                       | `internal=True` flag set in Step 2 |

---

## Complete example: "find BP named Jumbo"

```
User: "Find business partners with Jumbo in their name"
  │
  ▼  LLM selects tool
business_partner_filter_A_BusinessPartner
  arguments: { "filter": "substringof('Jumbo',BusinessPartner) eq true" }
  │
  ▼  bridge.handle() → svc.filter()
GET .../A_BusinessPartner?$format=json&$filter=substringof('Jumbo',BusinessPartner)%20eq%20true&$top=2
  │
  ▼  SAP responds
{ "d": { "results": [{ "BusinessPartner": "0010056789", "CreationDate": "/Date(1609459200000)/", ... }] } }
  │
  ▼  normalise
{ "value": [{ "BusinessPartner": "0010056789", "CreationDate": "2021-01-01T00:00:00Z", ... }] }
  │
  ▼  MCP tool result → LLM → User
"Found 1 business partner: 0010056789, created on 2021-01-01"
```

---

## OData v4 action example (EAN service)

For the `ean_e1` service, the metadata contains a bound Action:

```xml
<Action Name="GenerateEAN_E1" IsBound="true">
  <Parameter Name="it_Material" Type="Collection(API_EAN.ZSD_JAM_EAN_E1Type)"/>
  <Parameter Name="IV_Plant"    Type="Edm.String"/>
  <Parameter Name="IV_Quantity" Type="Edm.Int32"/>
</Action>
```

This becomes a single action tool:

```json
{
  "name": "ean_e1_action_GenerateEAN_E1",
  "description": "[ean_e1] Action 'GenerateEAN_E1' on collection ...",
  "inputSchema": {
    "properties": {
      "IV_Plant":    { "type": "string",  "description": "32-bit integer — pass as number." },
      "IV_Quantity": { "type": "integer", "description": "32-bit integer." }
    }
  }
}
```

Called as:

```
POST .../GenerateEAN_E1(...)
     IV_Plant='1000'&IV_Quantity=100
```

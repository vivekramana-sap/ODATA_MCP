# Tool Output Comparison: Python v2 vs Go

**Test EDMX:** SAP API_BUSINESS_PARTNER (OData v2, 43 entity sets, 0 function imports, ~489 KB)

---

## High-Level Numbers

| Metric | Python v2 | Go |
|---|---|---|
| **Total tools generated** | **285** | **242** |
| `info` | 1 | 1 |
| `schema` | **43** | **0** |
| `filter` | 43 | 43 |
| `count` | 43 | 43 |
| `search` | **0** | **0** |
| `get` | 43 | 43 |
| `create` | 38 | 38 |
| `update` | 42 | 42 |
| `delete` | 32 | 32 |

**Key diff:** Python v2 generates 43 extra `schema` tools (one per entity set) that dump every field, type, and SAP label — Go has no equivalent.

---

## Tool-by-Tool: `A_BusinessPartner`

### 1. Service Info

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp__info` | `odata_service_info_for_od` |
| **Description** (chars) | 175 | 89 |
| **Params** | 0 | 1 (`include_metadata: boolean`) |
| **Content** | URL, entity sets, actions, enabled ops, auth type, response limits | Generic "metadata, entity sets, capabilities" |

Python's info is **more descriptive** and contextual ("Call this first to understand what tools are available").

---

### 2. Filter

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp_filter_A_BusinessPartner` | `filter_A_BusinessPartner_for_od` |
| **Description** | 438 chars — includes default/max row counts, lists all expandable nav properties, key fields, pagination hints, cross-reference to count tool | 63 chars — generic "List/filter with OData query options" |
| **Params** | 7 (no `$` prefix) | 7 (with `$` prefix) |

#### Parameter naming
- **Python:** `filter`, `select`, `top`, `skip`, `orderby`, `expand`, `count` (no `$` prefix — LLM-friendly)
- **Go:** `$filter`, `$select`, `$top`, `$skip`, `$orderby`, `$expand`, `$count` (raw OData names)

#### Filter parameter description
- **Python (1,165 chars):** Full OData filter syntax tutorial with operators, logic, string functions, multi-value (`in`), null check, a concrete example using actual field names, and **all 63 filterable field names listed**
- **Go (23 chars):** `"OData filter expression"`

#### Other parameter descriptions
| Param | Python | Go |
|---|---|---|
| `top` | `"Max records to return. Default: 50. Max: 500."` | `"Maximum number of entities to return"` |
| `skip` | `"Records to skip for pagination. Use multiples of top to page through results."` | `"Number of entities to skip"` |
| `expand` | Lists all 10 nav property names: `to_BuPaIdentification, to_BuPaIndustry, ...` | `"Navigation properties to expand"` |
| `select` | **Lists all available field names** | `"Comma-separated list of properties to select"` |
| `orderby` | `"Sort expression. Example: BusinessPartner desc. Multiple: Field1 asc,Field2 desc"` | `"Properties to order by"` |

---

### 3. Count

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp_count_A_BusinessPartner` | `count_A_BusinessPartner_for_od` |
| **Description** | 114 chars — "Use before paginating to know how many pages to expect" | 60 chars — "Get count of entities with optional filter" |
| **Filter desc** | Full syntax tutorial + all field names (1,165 chars) | "OData filter expression" (23 chars) |

---

### 4. Get (single entity by key)

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp_get_A_BusinessPartner` | `get_A_BusinessPartner_for_od` |
| **Description** | 186 chars — names the key field, shows predicate example (`BusinessPartner='VALUE'`), cross-references filter tool for discovery | 44 chars — "Get a single entity by key" |
| **Params** | 3 (`BusinessPartner`, `select`, `expand`) | 3 (`BusinessPartner`, `$select`, `$expand`) |
| **Key param desc** | `"Business Partner."` (SAP label) | `"Key property: BusinessPartner"` (generic) |
| **`select` desc** | Lists all available fields | "Comma-separated list of properties to select" |
| **`expand` desc** | Lists all nav property names | "Navigation properties to expand" |

---

### 5. Create

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp_create_A_BusinessPartner` | `create_A_BusinessPartner_for_od` |
| **Description** | 146 chars — mentions key fields are required, non-nullable fields must be supplied | 37 chars — "Create a new entity" |
| **Params** | 68 properties, **1 required** | 67 properties, **0 required** |
| **`additionalProperties`** | `false` | not set |

#### Property descriptions (sample)

| Property | Python v2 | Go |
|---|---|---|
| `AcademicTitle` | `"Academic Title 1."` | `"Property: AcademicTitle"` |
| `BirthDate` | `"Date of Birth. Format: ISO-8601 (e.g. 2024-03-26T00:00:00)."` + `format: "date-time"` | `"Property: BirthDate"` |
| `BusinessPartnerUUID` | `"BP GUID. UUID — omit quotes..."` + `format: "uuid"` | `"Property: BusinessPartnerUUID"` |
| `IsFemale` | `type: boolean`, `"Female."` | `type: boolean`, `"Property: BusinessPartnerIsBlocked"` |
| `CreationDate` | `format: "date-time"` | no format |
| `BusPartMaritalStatus` | `"Marital Status."` | `"Property: BusPartMaritalStatus"` |

**SAP label coverage:** Python = **61/68** fields have SAP labels. Go = **0/67** (all use generic `"Property: FieldName"`).  
**Format hints:** Python = **7** fields with `format: date-time` or `format: uuid`. Go = **0**.

---

### 6. Update

| | Python v2 | Go |
|---|---|---|
| **Name** | `bp_update_A_BusinessPartner` | `update_A_BusinessPartner_for_od` |
| **Description** | 144 chars — "Only fields you supply are changed — omitted fields are untouched" | 43 chars — "Update an existing entity" |
| **Params** | 69 (68 fields + `_method` for PATCH/MERGE/PUT) | 69 (68 fields + `BusinessPartner` key) |
| **SAP labels** | 62/69 | 2/69 |
| **Format hints** | 7 | 0 |

Python includes `_method` param to choose PATCH vs MERGE vs PUT — Go doesn't expose this.

---

### 7. Schema (Python-only)

Python generates `bp_schema_A_BusinessPartner` with **0 params** — it returns full field inventory with types, labels, maxLength, nullable flags, navigation properties, key predicates, and sap:* annotations. Go has no equivalent tool.

---

## Summary of Differences

| Aspect | Python v2 | Go |
|---|---|---|
| **Extra tool type** | `schema` tools (43 extra) | None |
| **Parameter naming** | No `$` prefix (LLM-friendly) | `$` prefix (raw OData) |
| **Description richness** | 3–10× more descriptive, includes examples, cross-references, field lists | Terse, generic descriptions |
| **SAP annotations** | `sap:label` mapped to descriptions, `sap:creatable/updatable/deletable` check | Not extracted |
| **Type hints** | `format: date-time`, `format: uuid`, `type: boolean` for Edm types | All mapped to `string` or `boolean`, no `format` |
| **Field lists in filter/select** | Embeds all field names in parameter descriptions | No field names in descriptions |
| **Nav properties** | Listed explicitly in filter description and expand param | Not listed |
| **Pagination guidance** | Default/max row counts, cross-reference to count tool | None |
| **`additionalProperties: false`** | Set on all tools | Not set |
| **Required fields** | Marks key fields as required in create/update | No required fields in create |
| **Delete capability** | Respects `sap:deletable="false"` (no delete tool for A_BusinessPartner) | Same behavior |

### What this means for an LLM consumer

The Python v2 tools are **self-documenting** — an LLM can read the filter tool and immediately know:
- What fields exist and what they're called in SAP terms
- How to construct filter expressions with correct syntax
- Which navigation properties can be expanded
- How many records to expect and how to paginate
- What format dates should be in

With the Go tools, the LLM must **discover field names by calling the tool first** (or guessing), has no filter syntax guidance, and doesn't know which nav properties are available without trial and error.

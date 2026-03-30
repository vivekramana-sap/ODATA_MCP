"""
Bridge — manages multiple OData services, generates & dispatches MCP tools.
"""

import json
import re
import sys
import traceback

from .constants import (
    OP_FILTER,
    OP_SEARCH,
    OP_GET,
    OP_CREATE,
    OP_UPDATE,
    OP_DELETE,
    OP_ACTION,
)
from .helpers import _guard_params, safe_prop_name
from .odata_service import ODataService

_TOOL_NAME_RE = re.compile(r'[^a-zA-Z0-9_-]+')

_TYPE_HINTS: dict = {
    "Edm.Date":           ("Format: YYYY-MM-DD (e.g. 2024-03-26).", "date"),
    "Edm.DateTimeOffset": ("Format: ISO-8601 with timezone (e.g. 2024-03-26T00:00:00Z).", "date-time"),
    "Edm.DateTime":       ("Format: ISO-8601 (e.g. 2024-03-26T00:00:00).", "date-time"),
    "Edm.TimeOfDay":      ("Format: HH:MM:SS (e.g. 08:30:00).", "time"),
    "Edm.Guid":           ("UUID \u2014 omit quotes in key predicates (e.g. Id=12345678-abcd-1234-ef00-123456789abc).", "uuid"),
    "Edm.Decimal":        ("Decimal \u2014 use string format '9.99' for precision; avoid float literals.", None),
    "Edm.Double":         ("Double-precision float (e.g. 3.14).", None),
    "Edm.Single":         ("Single-precision float (e.g. 3.14).", None),
    "Edm.Int64":          ("64-bit integer \u2014 pass as number, no quotes.", None),
    "Edm.Int32":          ("32-bit integer \u2014 pass as number, no quotes.", None),
    "Edm.Int16":          ("16-bit integer \u2014 pass as number, no quotes.", None),
    "Edm.Byte":           ("Unsigned 8-bit integer (0\u2013255).", None),
    "Edm.SByte":          ("Signed 8-bit integer (\u2212128\u2013127).", None),
    "Edm.Binary":         ("Base64url-encoded binary string.", None),
}

_SAP_HINTS: dict = {
    "plant": "1000", "werks": "1000",
    "company": "1000", "bukrs": "1000",
    "salesorg": "1000", "vkorg": "1000",
    "material": "MAT-001", "matnr": "MAT-001",
    "vendor": "V-001", "lifnr": "V-001",
    "customer": "C-001", "kunnr": "C-001",
    "ean": "1234567890128",
    "year": "2024", "gjahr": "2024",
    "period": "01", "monat": "01",
}


def _pop_key(svc: ODataService, target: str, args: dict) -> str:
    """Pop key fields from args and return a comma-joined OData key predicate."""
    return ",".join(
        svc._wrap_guid_key(target, k, args.pop(k, ""))
        for k in svc.entity_sets[target]["keys"]
    )


class Bridge:
    def __init__(
        self,
        services:   list,
        sort_tools: bool = True,
        verbose:    bool = False,
    ):
        self.services:   dict[str, ODataService] = {s.alias: s for s in services}
        self.sort_tools: bool                    = sort_tools
        self.verbose:    bool                    = verbose

        self._all_tools: list       = []
        self._tool_map:  dict       = {}  # tool_name -> (svc, op, target)
        self._prop_maps: dict       = {}  # tool_name -> {safe_name: original_name}
        self._build_tools()

    # ------------------------------------------------------------------ #
    # Tool generation                                                      #
    # ------------------------------------------------------------------ #

    def _build_tools(self) -> None:
        tools: list = []
        for svc in self.services.values():
            for t in self._gen_tools(svc):
                tools.append(t)
                self._index_tool(svc, t["name"])
        if self.sort_tools:
            tools.sort(key=lambda t: t["name"])
        self._all_tools = tools


    @staticmethod
    def _safe_alias(alias: str) -> str:
        """Sanitize an alias for use in tool names (letters, digits, _ and - only)."""
        safe = _TOOL_NAME_RE.sub('_', alias).strip('_')
        return safe[:40] or 'svc'

    def _index_tool(self, svc: ODataService, name: str) -> None:
        prefix = self._safe_alias(svc.alias)
        rest = name[len(prefix) + 1:]
        for op in ("filter", "search", "count", "get", "create", "update", "delete", "action"):
            if rest.startswith(op + "_"):
                self._tool_map[name] = (svc, op, rest[len(op) + 1:])
                return

    # ------------------------------------------------------------------ #
    # Rich schema helpers                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _prop_schema(pname: str, pinfo: dict) -> dict:
        """Build a rich JSON Schema property dict for a single OData property."""
        edm_type  = pinfo.get("edm_type", "Edm.String")
        json_type = pinfo.get("type", "string")
        label     = pinfo.get("label", "")
        nullable  = pinfo.get("nullable", True)
        is_key    = pinfo.get("is_key", False)

        desc_parts: list = []
        extra: dict = {}

        if label and label != pname:
            desc_parts.append(label + ".")

        if edm_type in _TYPE_HINTS:
            hint, fmt = _TYPE_HINTS[edm_type]
            desc_parts.append(hint)
            if fmt:
                extra["format"] = fmt

        if not nullable and not is_key:
            desc_parts.append("Required — server rejects null.")

        schema: dict = {"type": json_type}
        if desc_parts:
            schema["description"] = " ".join(desc_parts)
        schema.update(extra)
        return schema

    @staticmethod
    def _key_predicate_hint(keys: list, props: dict) -> str:
        """Return a typed, realistic key predicate example string."""
        parts: list = []
        for k in keys:
            edm = props.get(k, {}).get("edm_type", "Edm.String")
            lower_k = k.lower()
            if edm in ("Edm.Int16", "Edm.Int32", "Edm.Int64",
                       "Edm.Byte", "Edm.SByte"):
                hint = _SAP_HINTS.get(lower_k, "1")
                parts.append(f"{k}={hint}")
            elif edm == "Edm.Guid":
                parts.append(f"{k}=12345678-abcd-1234-ef00-123456789abc")
            elif edm == "Edm.Boolean":
                parts.append(f"{k}=true")
            elif edm in ("Edm.Double", "Edm.Single", "Edm.Decimal"):
                parts.append(f"{k}=1.00")
            else:
                hint = _SAP_HINTS.get(lower_k, "VALUE")
                parts.append(f"{k}='{hint}'")
        return ",".join(parts) if parts else "Id='VALUE'"

    @staticmethod
    def _filter_desc(public_props: dict, keys: list, odata_version: str = "4") -> str:
        """Build a $filter description with type-aware examples."""
        is_v2 = odata_version == "2"
        ordered = (
            [(k, public_props[k]) for k in keys if k in public_props]
            + [(k, v) for k, v in public_props.items() if k not in keys]
        )
        examples: list = []
        for pname, pinfo in ordered:
            edm = pinfo.get("edm_type", "Edm.String")
            if edm in ("Edm.Int16", "Edm.Int32", "Edm.Int64",
                       "Edm.Byte", "Edm.SByte"):
                examples.append(f"{pname} eq 1")
            elif edm == "Edm.String":
                if is_v2:
                    examples.append(f"substringof('ABC',{pname}) eq true")
                else:
                    examples.append(f"contains({pname},'ABC')")
            elif edm == "Edm.Date":
                examples.append(f"{pname} ge 2024-01-01")
            elif edm == "Edm.DateTime":
                if is_v2:
                    examples.append(f"{pname} ge datetime'2024-01-01T00:00:00'")
                else:
                    examples.append(f"{pname} ge 2024-01-01T00:00:00Z")
            elif edm == "Edm.DateTimeOffset":
                examples.append(f"{pname} ge 2024-01-01T00:00:00Z")
            elif edm == "Edm.Boolean":
                examples.append(f"{pname} eq true")
            elif edm in ("Edm.Decimal", "Edm.Double", "Edm.Single"):
                examples.append(f"{pname} gt 0.00")
            elif edm == "Edm.Guid":
                examples.append(f"{pname} eq 12345678-abcd-1234-ef00-123456789abc")
            if len(examples) >= 2:
                break

        example_str = " and ".join(examples) if examples else (
            f"{keys[0]} eq 'VALUE'" if keys else "Field eq 'VALUE'"
        )
        field_list  = ", ".join(public_props.keys())
        dt_note = (
            "DateTime: datetime'2024-01-01T00:00:00'. "
            if is_v2 else
            "DateTime: 2024-01-01T00:00:00Z. "
        )
        str_note = (
            "String: substringof('v',F) eq true, startswith(F,'v'). "
            if is_v2 else
            "String: contains(F,'v'), startswith(F,'v'), tolower(F) eq 'abc'. "
        )
        return (
            f"OData $filter expression. "
            f"Operators: eq, ne, lt, le, gt, ge. "
            f"Logic: and, or, not. "
            f"{str_note}"
            f"{dt_note}"
            f"Null: F eq null. "
            f"Example: {example_str}. "
            f"Fields: {field_list}."
        )

    @staticmethod
    def _make_tool(name: str, desc: str, props: dict, required: list = None) -> dict:
        """Build a fully-formed MCP tool dict with additionalProperties: false."""
        schema_props: dict = {}
        for k, v in props.items():
            schema_props[k] = {"type": v, "description": k} if isinstance(v, str) else v
        schema: dict = {
            "type":                 "object",
            "properties":           schema_props,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return {"name": name, "description": desc, "inputSchema": schema}

    def _gen_tools(self, svc: ODataService) -> list:
        tools:    list = []
        a = self._safe_alias(svc.alias)  # sanitized for tool names

        # ---- Service info tool ----
        tools.append(self._make_tool(
            f"{a}__info",
            (
                f"Returns metadata for OData service [{a}]: URL, entity sets, "
                "actions, enabled operations, auth type, and response limits. "
                "Call this first to understand what tools are available."
            ),
            {},
        ))
        self._tool_map[f"{a}__info"] = (svc, "info", a)

        # ---- Entity set tools ----
        for es_name, es in svc.entity_sets.items():
            keys      = es["keys"]
            props     = es["props"]
            nav_props = es.get("nav_props", [])
            caps      = es.get("capabilities", {})

            user_props    = {k: v for k, v in props.items() if not v.get("internal")}
            non_key_props = {k: v for k, v in user_props.items() if k not in keys}
            key_props     = {k: v for k, v in user_props.items() if k in keys}

            # Props that SAP allows in create/update bodies (v2 sap:creatable/updatable)
            creatable_non_key = {k: v for k, v in non_key_props.items() if v.get("sap_creatable", True)}
            updatable_non_key = {k: v for k, v in non_key_props.items() if v.get("sap_updatable", True)}

            field_list   = ", ".join(user_props.keys())
            key_hint     = self._key_predicate_hint(keys, props)
            filter_desc  = self._filter_desc(user_props, keys, svc.odata_version)
            expand_desc  = (
                f"Comma-separated navigation properties to inline in the response. "
                f"Use this to fetch related data in a single call instead of separate queries. "
                f"Available: {', '.join(nav_props)}."
                if nav_props else "Navigation properties to expand (none available for this entity)."
            )
            nav_hint = (
                f" To fetch related data in one call use expand= with: {', '.join(nav_props)}."
                if nav_props else ""
            )

            key_schema       = {safe_prop_name(k): self._prop_schema(k, {**v, "is_key": True}) for k, v in key_props.items()}
            creatable_schema = {safe_prop_name(k): self._prop_schema(k, v) for k, v in creatable_non_key.items()}
            updatable_schema = {safe_prop_name(k): self._prop_schema(k, v) for k, v in updatable_non_key.items()}

            key_map       = {safe_prop_name(k): k for k in key_props}
            creatable_map = {safe_prop_name(k): k for k in creatable_non_key}
            updatable_map = {safe_prop_name(k): k for k in updatable_non_key}

            # --- schema discovery tool ---
            # Only generate it when there are other tools for this entity set
            # that the LLM might want to explore — no point if nothing else exists.
            _any_read_op = (
                svc.op_filter.allows(OP_FILTER)
                or svc.op_filter.allows(OP_GET)
                or svc.op_filter.allows(OP_SEARCH)
            )
            if _any_read_op:
                tname_schema = f"{a}_schema_{es_name}"
                tools.append(self._make_tool(
                    tname_schema,
                    (
                        f"[{a}] Describe {es_name} fields — returns field names, types, "
                        f"SAP labels, key fields, computed flags and navigation properties. "
                        f"Call once to understand the structure before querying."
                    ),
                    {},
                ))
                self._tool_map[tname_schema] = (svc, "schema", es_name)

            # --- filter ---
            if svc.op_filter.allows(OP_FILTER):
                filter_props = {
                    "filter":  {"type": "string",  "description": filter_desc},
                    "select":  {"type": "string",  "description": f"Comma-separated fields to return. Reduces payload size. Available: {field_list}."},
                    "orderby": {"type": "string",  "description": f"Sort expression. Example: {keys[0] if keys else 'Field'} desc. Multiple: Field1 asc,Field2 desc."},
                    "expand":  {"type": "string",  "description": expand_desc},
                    "top":     {"type": "integer", "description": f"Max records to return. Default: {svc.default_top}. Max: {svc.max_top}.", "minimum": 1, "maximum": svc.max_top},
                    "skip":    {"type": "integer", "description": "Records to skip for pagination. Use multiples of top to page through results.", "minimum": 0},
                    "count":   {"type": "boolean", "description": "Set true to include @odata.count (total matching records) in the response."},
                }
                if caps.get("searchable", False):
                    filter_props["search"] = {"type": "string", "description": "OData $search — full-text keyword search across searchable fields (e.g. 'hammer' or '\"exact phrase\"'). Use filter for field-level precision; use search for keyword discovery."}

                tools.append(self._make_tool(
                    f"{a}_filter_{es_name}",
                    (
                        f"[{a}] Search/list/lookup {es_name} — use for any open-ended request "
                        f"or when you don't have an exact key value yet. "
                        f"Returns up to {svc.default_top} records by default "
                        f"(server max: {svc.max_top}). "
                        f"Key fields: {', '.join(keys) or 'none'}.{nav_hint} "
                        f"Use skip+top for pagination; call {a}_count_{es_name} for total count."
                    ),
                    filter_props,
                ))

            # --- search ---
            if svc.op_filter.allows(OP_SEARCH) and caps.get("searchable", False):
                tools.append(self._make_tool(
                    f"{a}_search_{es_name}",
                    (
                        f"[{a}] Full-text search {es_name}. "
                        f"Use for keyword discovery across searchable fields. "
                        f"For field-level precision, use {a}_filter_{es_name} instead."
                    ),
                    {
                        "search": {"type": "string",  "description": "Search query string (e.g. 'hammer' or '\"exact phrase\"')."},
                        "select": {"type": "string",  "description": f"Comma-separated fields to return. Available: {field_list}."},
                        "top":    {"type": "integer", "description": f"Max records to return. Default: {svc.default_top}.", "minimum": 1, "maximum": svc.max_top},
                    },
                    required=["search"],
                ))

            # --- count ---
            if svc.op_filter.allows(OP_FILTER) or svc.op_filter.allows(OP_GET):
                tools.append(self._make_tool(
                    f"{a}_count_{es_name}",
                    (
                        f"[{a}] Count {es_name} records, optionally filtered. "
                        f"Use before paginating to know how many pages to expect."
                    ),
                    {"filter": {"type": "string", "description": filter_desc}},
                ))

            # --- get ---
            if svc.op_filter.allows(OP_GET) and caps.get("gettable_by_key", True):
                tname = f"{a}_get_{es_name}"
                self._prop_maps[tname] = key_map
                tools.append(self._make_tool(
                    tname,
                    (
                        f"[{a}] Fetch one {es_name} record by its EXACT key — "
                        f"ONLY use this when you already have the precise key value(s) from a previous filter/search result. "
                        f"Do NOT use for lookups or open-ended requests; use {a}_filter_{es_name} instead. "
                        f"Requires ALL key field(s): {', '.join(keys) or 'none'}. "
                        f"Example: {key_hint}."
                    ),
                    {
                        **key_schema,
                        "select": {"type": "string", "description": f"Fields to return. Available: {field_list}."},
                        "expand": {"type": "string", "description": expand_desc},
                    },
                    required=list(key_schema.keys()),
                ))

            # --- create ---
            if svc.op_filter.allows(OP_CREATE) and caps.get("creatable", True):
                tname = f"{a}_create_{es_name}"
                create_required = [
                    safe_prop_name(k) for k, v in creatable_non_key.items()
                    if not v.get("nullable", True)
                ]
                key_required_for_create = [
                    safe_prop_name(k) for k in keys
                    if not key_props.get(k, {}).get("nullable", True)
                ]
                create_schema = {**key_schema, **creatable_schema} if key_required_for_create else creatable_schema
                create_prop_map = {**key_map, **creatable_map} if key_required_for_create else creatable_map
                all_create_required = (key_required_for_create + create_required) or None
                self._prop_maps[tname] = create_prop_map
                key_note = (
                    f"Key fields ({', '.join(keys)}) are required — supply them."
                    if key_required_for_create else
                    f"Key fields ({', '.join(keys) or 'none'}) are optional — omit for server-generated keys."
                )
                tools.append(self._make_tool(
                    tname,
                    (
                        f"[{a}] Create a new {es_name} record. "
                        f"{key_note} "
                        f"Non-nullable non-key fields must be supplied."
                    ),
                    create_schema,
                    required=all_create_required,
                ))

            # --- update ---
            if svc.op_filter.allows(OP_UPDATE) and caps.get("updatable", True):
                tname = f"{a}_update_{es_name}"
                self._prop_maps[tname] = {**key_map, **updatable_map}
                update_method_schema = {
                    "_method": {
                        "type":        "string",
                        "description": "HTTP method. Use PATCH for partial update (default), "
                                       "MERGE for SAP OData v2 partial update, "
                                       "PUT for full entity replacement.",
                        "enum":        ["PATCH", "MERGE", "PUT"],
                        "default":     "PATCH",
                    },
                }
                tools.append(self._make_tool(
                    tname,
                    (
                        f"[{a}] Update {es_name}. "
                        f"Only fields you supply are changed — omitted fields are untouched. "
                        f"Supply each key field as a separate parameter."
                    ),
                    {
                        **key_schema,
                        **updatable_schema,
                        **update_method_schema,
                    },
                    required=list(key_schema.keys()),
                ))

            # --- delete ---
            if svc.op_filter.allows(OP_DELETE) and caps.get("deletable", True):
                tname = f"{a}_delete_{es_name}"
                self._prop_maps[tname] = dict(key_map)
                tools.append(self._make_tool(
                    tname,
                    (
                        f"[{a}] Permanently delete a {es_name} record. This is irreversible. "
                        f"Supply each key field as a separate parameter."
                    ),
                    key_schema,
                    required=list(key_schema.keys()),
                ))

        # ---- Action / Function tools ----
        if svc.op_filter.allows(OP_ACTION):
            for action in svc.actions:
                a_name   = action["name"]
                es       = action.get("entity_set", "")
                is_bound = action.get("is_bound", False)
                is_coll  = action.get("is_collection_bound", False)
                is_v2_fn = action.get("is_v2_function", False)
                http_meth = action.get("http_method", "POST")

                if is_v2_fn and http_meth != "GET" and svc.op_filter.allows(OP_ACTION) and svc.readonly:
                    continue

                if is_v2_fn:
                    desc = (
                        f"[{a}] Function '{a_name}' ({http_meth}). "
                        f"OData v2 function import \u2014 calls a server-side operation."
                    )
                elif is_bound and es:
                    desc = (
                        f"[{a}] Action '{a_name}' on {'collection' if is_coll else 'entity'} {es}. "
                        f"Triggers a business operation that cannot be expressed as a standard CRUD call."
                    )
                else:
                    desc = f"[{a}] Unbound action '{a_name}'. Triggers a server-side business operation."

                p_props:   dict = {}
                param_map: dict = {}
                required_params: list = []

                if is_bound and es and not is_coll:
                    es_keys  = svc.entity_sets.get(es, {}).get("keys", [])
                    es_props = svc.entity_sets.get(es, {}).get("props", {})
                    p_props["_entity_key"] = {
                        "type":        "string",
                        "description": (
                            f"Key predicate of the {es} instance to act on. "
                            f"Example: {self._key_predicate_hint(es_keys, es_props)}."
                        ),
                    }
                    required_params.append("_entity_key")

                for p in action["params"]:
                    safe = safe_prop_name(p["name"])
                    param_map[safe] = p["name"]
                    p_props[safe]   = self._prop_schema(p["name"], {
                        "edm_type": p.get("edm_type", "Edm.String"),
                        "type":     p["type"],
                        "label":    p.get("label", ""),
                        "nullable": True,
                        "is_key":   False,
                    })

                tname = f"{a}_action_{a_name}"
                self._prop_maps[tname] = param_map
                tools.append(self._make_tool(
                    tname,
                    desc,
                    p_props,
                    required=required_params or None,
                ))

        return tools

    # ------------------------------------------------------------------ #
    # MCP dispatch                                                         #
    # ------------------------------------------------------------------ #

    def handle(self, req: dict, auth_header: str = "") -> dict | None:
        method  = req.get("method", "")
        req_id  = req.get("id")
        params  = req.get("params") or {}

        def ok(result):
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        def err(code, msg):
            return {
                "jsonrpc": "2.0",
                "id":      req_id,
                "error":   {"code": code, "message": msg},
            }

        if method == "initialize":
            return ok({
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "serverInfo":      {"name": "odata-mcp-bridge", "version": "2.0.0"},
                "capabilities":    {"tools": {}},
            })

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "tools/list":
            return ok({"tools": self._all_tools})

        if method == "tools/call":
            tool_name = params.get("name", "")
            args      = _guard_params(dict(params.get("arguments", {})))

            entry = self._tool_map.get(tool_name)
            if not entry:
                return err(-32601, f"Unknown tool: {tool_name}")

            svc, op, target = entry

            prop_map = self._prop_maps.get(tool_name, {})
            if prop_map:
                args = {prop_map.get(k, k): v for k, v in args.items()}

            _odata_sys = {"filter", "top", "skip", "select", "orderby", "expand", "search"}
            args = {
                (f"${k}" if k in _odata_sys else k): v
                for k, v in args.items()
            }

            try:
                if op == "info":
                    a_ = self._safe_alias(svc.alias)
                    result = {
                        "alias":              svc.alias,
                        "url":                svc.url,
                        "odata_version":      svc.odata_version,
                        "schema_namespace":   svc.schema_ns,
                        "entity_sets":        list(svc.entity_sets.keys()),
                        "actions":            [a["name"] for a in svc.actions],
                        "auth_type":          (
                            "passthrough" if svc.passthrough
                            else "cookie"     if svc._extra_cookies
                            else "basic"      if svc.username
                            else "anonymous"
                        ),
                        "max_items":          svc.max_items,
                        "max_response_bytes": svc.max_response_size,
                        "legacy_dates":       svc.legacy_dates,
                        "_tool_guide": (
                            f"For any lookup or open-ended request use {a_}_filter_<EntitySet>. "
                            f"Use {a_}_get_<EntitySet> ONLY when you already have the exact key. "
                            f"Use {a_}_schema_<EntitySet> to see field names and types before filtering."
                        ),
                    }

                elif op == "schema":
                    es    = svc.entity_sets.get(target, {})
                    props = es.get("props", {})
                    keys  = es.get("keys", [])
                    nav   = es.get("nav_props", [])
                    caps  = es.get("capabilities", {})
                    a_    = self._safe_alias(svc.alias)
                    # Build hint only for tools that actually exist
                    _possible = []
                    if svc.op_filter.allows(OP_FILTER):  _possible.append(f"{a_}_filter_{target}")
                    if svc.op_filter.allows(OP_GET):     _possible.append(f"{a_}_get_{target}")
                    if svc.op_filter.allows(OP_CREATE) and caps.get("creatable", True):
                        _possible.append(f"{a_}_create_{target}")
                    if svc.op_filter.allows(OP_UPDATE) and caps.get("updatable", True):
                        _possible.append(f"{a_}_update_{target}")
                    if svc.op_filter.allows(OP_DELETE) and caps.get("deletable", True):
                        _possible.append(f"{a_}_delete_{target}")
                    result = {
                        "entity_set": target,
                        "keys":       keys,
                        "capabilities": caps,
                        "fields": [
                            {
                                "name":     k,
                                "type":     v["edm_type"],
                                "label":    v.get("label") or k,
                                "nullable": v["nullable"],
                                "key":      k in keys,
                                "computed": v.get("computed", False),
                            }
                            for k, v in props.items()
                            if not v.get("internal")
                        ],
                        "nav_props":  nav,
                        "_mcp_hint":  f"Available tools for this entity: {', '.join(_possible)}" if _possible else "No tools available for this entity.",
                    }

                elif op == "filter":
                    result = svc.filter(target, args, auth=auth_header)

                elif op == "search":
                    result = svc.search(target, args, auth=auth_header)

                elif op == "count":
                    result = svc.count(
                        target,
                        args.get("$filter", args.get("filter", "")),
                        auth=auth_header,
                    )

                elif op == "get":
                    result = svc.get(target, _pop_key(svc, target, args), args, auth=auth_header)

                elif op == "create":
                    result = svc.create(target, args, auth=auth_header)

                elif op == "update":
                    key = _pop_key(svc, target, args)
                    http_method = args.pop("_method", "PATCH")
                    if http_method not in ("PATCH", "MERGE", "PUT"):
                        http_method = "PATCH"
                    result = svc.update(target, key, args, method=http_method, auth=auth_header)

                elif op == "delete":
                    result = svc.delete(target, _pop_key(svc, target, args), auth=auth_header)

                elif op == "action":
                    args.pop("_entity_key", None)
                    result = svc.call_action(target, args, auth=auth_header)

                else:
                    return err(-32601, f"Unknown op: {op}")

            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                return err(-32603, f"Internal error: {exc}")

            return ok({
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ]
            })

        if method == "ping":
            return ok({})

        return err(-32601, f"Method not found: {method}")

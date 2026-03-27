"""
Constants shared across the OData MCP Bridge modules.
"""

import re

# OData EDM namespace (v4)
EDM_NS       = "http://docs.oasis-open.org/odata/ns/edm"
HTTP_TIMEOUT = 30

# EDM type → JSON Schema type mapping
EDM_TO_JSON: dict[str, str] = {
    "Edm.String":         "string",
    "Edm.Int16":          "integer",
    "Edm.Int32":          "integer",
    "Edm.Int64":          "integer",
    "Edm.Decimal":        "number",
    "Edm.Double":         "number",
    "Edm.Single":         "number",
    "Edm.Boolean":        "boolean",
    "Edm.Date":           "string",
    "Edm.DateTimeOffset": "string",
    "Edm.TimeOfDay":      "string",
    "Edm.DateTime":       "string",   # OData v2 compat
    "Edm.Guid":           "string",
    "Edm.Binary":         "string",
    "Edm.Byte":           "integer",
    "Edm.SByte":          "integer",
}

# Operation codes — mirror Go implementation
OP_CREATE = "C"
OP_SEARCH = "S"
OP_FILTER = "F"
OP_GET    = "G"
OP_UPDATE = "U"
OP_DELETE = "D"
OP_ACTION = "A"
OP_READ   = "R"   # shorthand that expands to S + F + G

# SAP legacy date pattern:  /Date(1748736000000+0000)/
_LEGACY_DATE_RE = re.compile(r'/Date\((-?\d+)([+-]\d{4})?\)/')

# GUID pattern for SAP v2 guid'...' wrapping
_GUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

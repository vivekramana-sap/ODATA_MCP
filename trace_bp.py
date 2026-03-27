#!/usr/bin/env python3
"""Generate trace output for Python v2 bridge against Business Partner EDMX."""
import json, sys
from unittest.mock import patch, MagicMock
import urllib.request

EDMX_URL = (
    "https://raw.githubusercontent.com/Azure-Samples/"
    "app-service-javascript-sap-cloud-sdk-quickstart/main/src/api/API_BUSINESS_PARTNER.edmx"
)

sys.path.insert(0, ".")
from bridge_core.odata_service import ODataService
from bridge_core.bridge import Bridge

data = urllib.request.urlopen(EDMX_URL, timeout=30).read()
sys.stderr.write(f"Downloaded {len(data):,} bytes\n")

mock_resp = MagicMock()
mock_resp.read.return_value = data
mock_resp.__enter__ = MagicMock(return_value=mock_resp)
mock_resp.__exit__ = MagicMock(return_value=False)

with patch.object(ODataService, "_open", return_value=mock_resp):
    svc = ODataService(
        alias="bp",
        url="https://example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER",
        username="test", password="test",
    )

bridge = Bridge([svc])
tools = bridge._all_tools

# Write full JSON to file for reference
with open("trace_python_bp.json", "w") as f:
    json.dump(tools, f, indent=2)

print(f"Total tools: {len(tools)}")
print(f"OData version detected: v{svc.odata_version}")
print(f"Entity sets: {len(svc.entity_sets)}")
print(f"Actions/Functions: {len(svc.actions)}")
print()

# Show representative samples
sample_names = [
    "bp__info",
    "bp_schema_A_BusinessPartner",
    "bp_filter_A_BusinessPartner",
    "bp_count_A_BusinessPartner",
    "bp_get_A_BusinessPartner",
    "bp_create_A_BusinessPartner",
    "bp_update_A_BusinessPartner",
    "bp_delete_A_BusinessPartner",
]

for name in sample_names:
    t = next((t for t in tools if t["name"] == name), None)
    if t:
        print(f"{'='*80}")
        print(json.dumps(t, indent=2))
    else:
        print(f"{'='*80}")
        print(f"[NOT FOUND] {name}")

print(f"\n{'='*80}")
print("All tool names:")
for t in tools:
    props = list(t.get("inputSchema", {}).get("properties", {}).keys())
    print(f"  {t['name']}  params={props}")

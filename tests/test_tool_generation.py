#!/usr/bin/env python3
"""
Tests for tool generation correctness across OData v2 and v4.

Covers:
  1. V4 — computed keys → create suppressed, get kept
  2. V4 — external <Annotations> capability restrictions (SearchRestrictions,
           InsertRestrictions, UpdateRestrictions, DeleteRestrictions)
  3. V2 — sap:creatable/updatable/deletable="false" inline attributes
  4. V2 hybrid — external OData-v4-ns <Annotations> blocks inside v2 metadata
  5. Schema tool — only generated when at least one read op exists
  6. Filter tool description — uses "USE THIS" wording for open-ended requests
  7. Get tool description — leads with "EXACT key" / "ONLY use" constraint
  8. _mcp_hint in schema result — only lists tools that actually exist
  9. Schema response — includes "computed" flag per field
 10. Bridge __info result — contains _tool_guide
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")
from bridge_core.odata_service import ODataService
from bridge_core.bridge import Bridge


# ---------------------------------------------------------------------------
# Helper: build a service from raw EDMX bytes without any real HTTP
# ---------------------------------------------------------------------------

def _make_svc(edmx: bytes, alias: str = "svc", **kwargs) -> ODataService:
    mock_resp = MagicMock()
    mock_resp.content = edmx
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    with patch("bridge_core.odata_service.requests.Session") as MockSession:
        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp
        MockSession.return_value = mock_sess
        return ODataService(
            alias=alias,
            url="https://example.com/sap/opu/odata/test",
            username="u",
            password="p",
            **kwargs,
        )


def _tool_names(svc: ODataService) -> list:
    bridge = Bridge([svc])
    return [t["name"] for t in bridge._all_tools]


def _tool(svc: ODataService, name_suffix: str) -> dict | None:
    """Find a tool by its suffix (part after the alias prefix)."""
    bridge = Bridge([svc])
    a = Bridge._safe_alias(svc.alias)
    full = f"{a}_{name_suffix}" if not name_suffix.startswith(a) else name_suffix
    for t in bridge._all_tools:
        if t["name"] == full:
            return t
    return None


# ---------------------------------------------------------------------------
# V4 EDMX fixtures
# ---------------------------------------------------------------------------

V4_COMPUTED_KEYS = b"""\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
<edmx:DataServices>
<Schema Namespace="com.example" Alias="SAP__self"
        xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="EanType">
    <Key><PropertyRef Name="Ean"/><PropertyRef Name="Type"/></Key>
    <Property Name="Ean"  Type="Edm.String" Nullable="false"/>
    <Property Name="Type" Type="Edm.String" Nullable="false"/>
    <Property Name="Name" Type="Edm.String"/>
  </EntityType>
  <EntityContainer Name="Container">
    <EntitySet Name="EanSet" EntityType="com.example.EanType"/>
  </EntityContainer>
  <Annotations Target="SAP__self.EanType/Ean">
    <Annotation Term="SAP__core.Computed"/>
  </Annotations>
  <Annotations Target="SAP__self.EanType/Type">
    <Annotation Term="SAP__core.Computed"/>
  </Annotations>
</Schema>
</edmx:DataServices>
</edmx:Edmx>"""

V4_INSERT_RESTRICTED = b"""\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
<edmx:DataServices>
<Schema Namespace="com.example" Alias="SAP__self"
        xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="StockType">
    <Key><PropertyRef Name="Material"/><PropertyRef Name="Plant"/></Key>
    <Property Name="Material" Type="Edm.String" Nullable="false"/>
    <Property Name="Plant"    Type="Edm.String" Nullable="false"/>
    <Property Name="Qty"      Type="Edm.Decimal"/>
  </EntityType>
  <EntityContainer Name="Container">
    <EntitySet Name="StockSet" EntityType="com.example.StockType"/>
  </EntityContainer>
  <Annotations Target="SAP__self.Container/StockSet">
    <Annotation Term="SAP__capabilities.InsertRestrictions">
      <Record><PropertyValue Property="Insertable" Bool="false"/></Record>
    </Annotation>
    <Annotation Term="SAP__capabilities.DeleteRestrictions">
      <Record><PropertyValue Property="Deletable" Bool="false"/></Record>
    </Annotation>
    <Annotation Term="SAP__capabilities.UpdateRestrictions">
      <Record>
        <PropertyValue Property="Updatable" Path="__EntityControl/Updatable"/>
      </Record>
    </Annotation>
    <Annotation Term="SAP__capabilities.SearchRestrictions">
      <Record><PropertyValue Property="Searchable" Bool="false"/></Record>
    </Annotation>
  </Annotations>
</Schema>
</edmx:DataServices>
</edmx:Edmx>"""

V4_FULLY_CAPABLE = b"""\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
<edmx:DataServices>
<Schema Namespace="com.example" Alias="SAP__self"
        xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="OrderType">
    <Key><PropertyRef Name="OrderId"/></Key>
    <Property Name="OrderId"   Type="Edm.String" Nullable="false"/>
    <Property Name="Status"    Type="Edm.String"/>
    <Property Name="Amount"    Type="Edm.Decimal"/>
  </EntityType>
  <EntityContainer Name="Container">
    <EntitySet Name="OrderSet" EntityType="com.example.OrderType"/>
  </EntityContainer>
  <Annotations Target="SAP__self.Container/OrderSet">
    <Annotation Term="SAP__capabilities.SearchRestrictions">
      <Record><PropertyValue Property="Searchable" Bool="true"/></Record>
    </Annotation>
  </Annotations>
</Schema>
</edmx:DataServices>
</edmx:Edmx>"""


# ---------------------------------------------------------------------------
# V2 EDMX fixtures
# ---------------------------------------------------------------------------

V2_ALL_READONLY = """\
<?xml version="1.0"?>
<edmx:Edmx Version="1.0"
  xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
  xmlns:sap="http://www.sap.com/Protocols/SAPData">
<edmx:DataServices>
<Schema Namespace="TEST"
        xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
  <EntityType Name="StockType" sap:label="Stock">
    <Key>
      <PropertyRef Name="Material"/>
      <PropertyRef Name="Plant"/>
    </Key>
    <Property Name="Material" Type="Edm.String" Nullable="false"
              sap:label="Material"/>
    <Property Name="Plant"    Type="Edm.String" Nullable="false"
              sap:label="Plant"/>
    <Property Name="Qty"      Type="Edm.Decimal" sap:label="Qty"/>
  </EntityType>
  <EntityContainer Name="Container" m:IsDefaultEntityContainer="true"
                   xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <EntitySet Name="StockSet" EntityType="TEST.StockType"
               sap:creatable="false"
               sap:updatable="false"
               sap:deletable="false"/>
  </EntityContainer>
</Schema>
</edmx:DataServices>
</edmx:Edmx>""".encode()

V2_CREATABLE = """\
<?xml version="1.0"?>
<edmx:Edmx Version="1.0"
  xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
  xmlns:sap="http://www.sap.com/Protocols/SAPData">
<edmx:DataServices>
<Schema Namespace="TEST"
        xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
  <EntityType Name="PurchaseOrderType" sap:label="PO">
    <Key><PropertyRef Name="POId"/></Key>
    <Property Name="POId"   Type="Edm.String" Nullable="false" sap:label="PO ID"/>
    <Property Name="Vendor" Type="Edm.String" sap:label="Vendor"/>
    <Property Name="Amount" Type="Edm.Decimal" sap:label="Amount"/>
  </EntityType>
  <EntityContainer Name="Container" m:IsDefaultEntityContainer="true"
                   xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <EntitySet Name="PurchaseOrderSet" EntityType="TEST.PurchaseOrderType"/>
  </EntityContainer>
</Schema>
</edmx:DataServices>
</edmx:Edmx>""".encode()

V2_HYBRID_EXTERNAL_ANNOTATIONS = (
    '<?xml version="1.0"?>'
    '<edmx:Edmx Version="1.0"'
    '  xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"'
    '  xmlns:sap="http://www.sap.com/Protocols/SAPData">'
    '<edmx:DataServices>'
    '<Schema Namespace="API_MATERIAL_STOCK_SRV"'
    '        xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
    '  <EntityType Name="A_MatlStkInAcctModType" sap:label="Stock">'
    '    <Key>'
    '      <PropertyRef Name="Material"/>'
    '      <PropertyRef Name="Plant"/>'
    '    </Key>'
    '    <Property Name="Material" Type="Edm.String" Nullable="false" sap:label="Material"/>'
    '    <Property Name="Plant"    Type="Edm.String" Nullable="false" sap:label="Plant"/>'
    '    <Property Name="Qty"      Type="Edm.Decimal" sap:label="Qty"/>'
    '  </EntityType>'
    '  <EntityContainer Name="API_MATERIAL_STOCK_SRV_Entities"'
    '    m:IsDefaultEntityContainer="true"'
    '    xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
    '    <EntitySet Name="A_MatlStkInAcctMod"'
    '               EntityType="API_MATERIAL_STOCK_SRV.A_MatlStkInAcctModType"'
    '               sap:creatable="false"'
    '               sap:updatable="false"'
    '               sap:deletable="false"/>'
    '  </EntityContainer>'
    '  <Annotations'
    '    Target="API_MATERIAL_STOCK_SRV.API_MATERIAL_STOCK_SRV_Entities/A_MatlStkInAcctMod"'
    '    xmlns="http://docs.oasis-open.org/odata/ns/edm">'
    '    <Annotation Term="SAP__capabilities.SearchRestrictions">'
    '      <Record><PropertyValue Property="Searchable" Bool="false"/></Record>'
    '    </Annotation>'
    '  </Annotations>'
    '</Schema>'
    '</edmx:DataServices>'
    '</edmx:Edmx>'
).encode()


# ===========================================================================
# Test classes
# ===========================================================================

class TestV4ComputedKeys(unittest.TestCase):
    """V4: all-computed keys → create suppressed, get still generated."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V4_COMPUTED_KEYS, alias="ean")

    def test_detected_as_v4(self):
        self.assertEqual(self.svc.odata_version, "4")

    def test_entity_set_loaded(self):
        self.assertIn("EanSet", self.svc.entity_sets)

    def test_key_properties_marked_computed(self):
        props = self.svc.entity_sets["EanSet"]["props"]
        self.assertTrue(props["Ean"].get("computed"), "Ean should be flagged computed")
        self.assertTrue(props["Type"].get("computed"), "Type should be flagged computed")

    def test_create_suppressed(self):
        caps = self.svc.entity_sets["EanSet"]["capabilities"]
        self.assertFalse(caps["creatable"], "Create should be False for all-computed keys")
        names = _tool_names(self.svc)
        self.assertNotIn("ean_create_EanSet", names, "create tool should NOT be generated")

    def test_get_still_generated(self):
        """GET by key is valid once you obtain the key from a filter result."""
        names = _tool_names(self.svc)
        self.assertIn("ean_get_EanSet", names, "get tool SHOULD still be generated")

    def test_filter_generated(self):
        names = _tool_names(self.svc)
        self.assertIn("ean_filter_EanSet", names)

    def test_schema_generated(self):
        names = _tool_names(self.svc)
        self.assertIn("ean_schema_EanSet", names)


class TestV4ExternalAnnotationCapabilities(unittest.TestCase):
    """V4: external <Annotations> blocks control create/delete/update/search."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V4_INSERT_RESTRICTED, alias="stock")

    def test_detected_as_v4(self):
        self.assertEqual(self.svc.odata_version, "4")

    def test_create_suppressed_by_insert_restriction(self):
        caps = self.svc.entity_sets["StockSet"]["capabilities"]
        self.assertFalse(caps["creatable"])
        names = _tool_names(self.svc)
        self.assertNotIn("stock_create_StockSet", names)

    def test_delete_suppressed_by_delete_restriction(self):
        caps = self.svc.entity_sets["StockSet"]["capabilities"]
        self.assertFalse(caps["deletable"])
        names = _tool_names(self.svc)
        self.assertNotIn("stock_delete_StockSet", names)

    def test_update_not_suppressed_when_path_based(self):
        """Dynamic path annotation (not static Bool) must not suppress update."""
        caps = self.svc.entity_sets["StockSet"]["capabilities"]
        self.assertTrue(caps.get("updatable", True),
                        "Updatable should remain True for dynamic path annotations")
        names = _tool_names(self.svc)
        self.assertIn("stock_update_StockSet", names)

    def test_search_not_generated_when_searchable_false(self):
        caps = self.svc.entity_sets["StockSet"]["capabilities"]
        self.assertFalse(caps.get("searchable", False))
        names = _tool_names(self.svc)
        self.assertNotIn("stock_search_StockSet", names)

    def test_filter_and_get_generated(self):
        names = _tool_names(self.svc)
        self.assertIn("stock_filter_StockSet", names)
        self.assertIn("stock_get_StockSet", names)


class TestV4SearchAnnotation(unittest.TestCase):
    """V4: SearchRestrictions Searchable=true → search tool generated."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V4_FULLY_CAPABLE, alias="orders")

    def test_searchable_True(self):
        caps = self.svc.entity_sets["OrderSet"]["capabilities"]
        self.assertTrue(caps["searchable"])

    def test_search_tool_generated(self):
        names = _tool_names(self.svc)
        self.assertIn("orders_search_OrderSet", names)

    def test_all_crud_tools_generated(self):
        names = _tool_names(self.svc)
        for suffix in ("filter_OrderSet", "get_OrderSet", "create_OrderSet",
                       "update_OrderSet", "delete_OrderSet", "schema_OrderSet"):
            self.assertIn(f"orders_{suffix}", names, f"Missing: orders_{suffix}")


class TestV2InlineCapabilities(unittest.TestCase):
    """V2: sap:creatable/updatable/deletable=false suppresses CUD tools."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V2_ALL_READONLY, alias="stock")

    def test_detected_as_v2(self):
        self.assertEqual(self.svc.odata_version, "2")

    def test_capabilities_parsed(self):
        caps = self.svc.entity_sets["StockSet"]["capabilities"]
        self.assertFalse(caps["creatable"])
        self.assertFalse(caps["updatable"])
        self.assertFalse(caps["deletable"])

    def test_no_cud_tools(self):
        names = _tool_names(self.svc)
        self.assertNotIn("stock_create_StockSet", names)
        self.assertNotIn("stock_update_StockSet", names)
        self.assertNotIn("stock_delete_StockSet", names)

    def test_read_tools_present(self):
        names = _tool_names(self.svc)
        self.assertIn("stock_filter_StockSet", names)
        self.assertIn("stock_get_StockSet", names)
        self.assertIn("stock_schema_StockSet", names)


class TestV2Creatable(unittest.TestCase):
    """V2: no sap:creatable restriction → CUD tools generated by default."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V2_CREATABLE, alias="po")

    def test_capabilities_default_to_true(self):
        caps = self.svc.entity_sets["PurchaseOrderSet"]["capabilities"]
        self.assertTrue(caps["creatable"])
        self.assertTrue(caps["updatable"])
        self.assertTrue(caps["deletable"])

    def test_all_crud_tools_generated(self):
        names = _tool_names(self.svc)
        for suffix in ("filter_PurchaseOrderSet", "get_PurchaseOrderSet",
                       "create_PurchaseOrderSet", "update_PurchaseOrderSet",
                       "delete_PurchaseOrderSet"):
            self.assertIn(f"po_{suffix}", names)


class TestV2HybridExternalAnnotations(unittest.TestCase):
    """V2 hybrid: external OData-v4-ns <Annotations> blocks inside v2 metadata."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V2_HYBRID_EXTERNAL_ANNOTATIONS, alias="stock")

    def test_detected_as_v2(self):
        self.assertEqual(self.svc.odata_version, "2")

    def test_inline_readonly_respected(self):
        caps = self.svc.entity_sets["A_MatlStkInAcctMod"]["capabilities"]
        self.assertFalse(caps["creatable"])
        self.assertFalse(caps["updatable"])
        self.assertFalse(caps["deletable"])

    def test_external_search_restriction_respected(self):
        caps = self.svc.entity_sets["A_MatlStkInAcctMod"]["capabilities"]
        self.assertFalse(caps.get("searchable", False))

    def test_no_cud_no_search(self):
        names = _tool_names(self.svc)
        self.assertNotIn("stock_create_A_MatlStkInAcctMod", names)
        self.assertNotIn("stock_search_A_MatlStkInAcctMod", names)

    def test_read_tools_present(self):
        names = _tool_names(self.svc)
        self.assertIn("stock_filter_A_MatlStkInAcctMod", names)
        self.assertIn("stock_get_A_MatlStkInAcctMod", names)


class TestSchemaToolConditions(unittest.TestCase):
    """Schema tool only generated when at least one read op exists."""

    def test_schema_tool_absent_when_read_only_is_false_for_all_ops(self):
        svc = _make_svc(V2_ALL_READONLY, alias="s")
        # Disable all ops
        from bridge_core.helpers import OpFilter
        svc.op_filter = OpFilter(disable_ops="SFGCUDA")
        names = _tool_names(svc)
        # With all ops disabled, no read ops → schema tool should not appear
        self.assertNotIn("s_schema_StockSet", names)

    def test_schema_tool_present_with_filter(self):
        svc = _make_svc(V2_ALL_READONLY, alias="s")
        names = _tool_names(svc)
        self.assertIn("s_schema_StockSet", names)

    def test_schema_result_includes_computed_flag(self):
        svc = _make_svc(V4_COMPUTED_KEYS, alias="ean")
        bridge = Bridge([svc])
        result = bridge.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ean_schema_EanSet", "arguments": {}}
        })
        fields = result["result"]["content"][0]["text"]
        import json
        data = json.loads(fields)
        field_map = {f["name"]: f for f in data["fields"]}
        self.assertTrue(field_map["Ean"]["computed"], "Ean should be computed=True")
        self.assertTrue(field_map["Type"]["computed"], "Type should be computed=True")
        self.assertFalse(field_map["Name"].get("computed", False), "Name should not be computed")

    def test_schema_mcp_hint_lists_existing_tools_only(self):
        """_mcp_hint in schema response must not mention create when it's suppressed."""
        svc = _make_svc(V4_COMPUTED_KEYS, alias="ean")
        bridge = Bridge([svc])
        result = bridge.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ean_schema_EanSet", "arguments": {}}
        })
        import json
        data = json.loads(result["result"]["content"][0]["text"])
        hint = data["_mcp_hint"]
        self.assertNotIn("create", hint.lower(), f"hint should not mention create: {hint}")
        self.assertIn("filter", hint.lower(), f"hint should mention filter: {hint}")
        self.assertIn("get", hint.lower(), f"hint should mention get: {hint}")


class TestToolDescriptions(unittest.TestCase):
    """Verify filter/get tool descriptions guide agents correctly."""

    @classmethod
    def setUpClass(cls):
        cls.svc = _make_svc(V4_FULLY_CAPABLE, alias="orders")
        cls.bridge = Bridge([cls.svc])
        cls.tools = {t["name"]: t for t in cls.bridge._all_tools}

    def test_filter_desc_mentions_open_ended(self):
        desc = self.tools["orders_filter_OrderSet"]["description"]
        self.assertIn("open-ended", desc,
                      f"filter desc should mention 'open-ended' to guide LLMs: {desc}")

    def test_filter_desc_mentions_open_ended_examples(self):
        desc = self.tools["orders_filter_OrderSet"]["description"]
        # Should contain guidance for open-ended requests
        self.assertTrue(
            any(phrase in desc for phrase in ("open-ended", "don't have", "don't know", "lookup")),
            f"filter desc should mention open-ended use cases: {desc}"
        )

    def test_get_desc_requires_exact_key(self):
        desc = self.tools["orders_get_OrderSet"]["description"]
        self.assertIn("EXACT", desc, f"get desc should contain 'EXACT': {desc}")

    def test_get_desc_says_only_use(self):
        desc = self.tools["orders_get_OrderSet"]["description"]
        self.assertIn("ONLY", desc, f"get desc should contain 'ONLY': {desc}")

    def test_get_desc_redirects_to_filter(self):
        desc = self.tools["orders_get_OrderSet"]["description"]
        self.assertIn("filter", desc.lower(),
                      f"get desc should redirect to filter: {desc}")

    def test_info_tool_contains_tool_guide(self):
        result = self.bridge.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "orders__info", "arguments": {}}
        })
        import json
        data = json.loads(result["result"]["content"][0]["text"])
        self.assertIn("_tool_guide", data, "info result should contain _tool_guide")
        guide = data["_tool_guide"]
        self.assertIn("filter", guide.lower())
        self.assertIn("ONLY", guide)

    def test_get_desc_differs_v2_vs_v4(self):
        """Both v2 and v4 get tools should have the same protective wording."""
        svc_v2 = _make_svc(V2_CREATABLE, alias="po")
        bridge_v2 = Bridge([svc_v2])
        tools_v2 = {t["name"]: t for t in bridge_v2._all_tools}
        desc_v2 = tools_v2["po_get_PurchaseOrderSet"]["description"]
        self.assertIn("EXACT", desc_v2)
        self.assertIn("ONLY", desc_v2)


class TestSchemaToolNotGeneratedForWriteOnly(unittest.TestCase):
    """Schema tool absent when filter, get, and search are all disabled."""

    def test_no_schema_when_all_read_ops_off(self):
        from bridge_core.helpers import OpFilter
        svc = _make_svc(V4_FULLY_CAPABLE, alias="o")
        svc.op_filter = OpFilter(disable_ops="SFGR")  # disable search, filter, get, read
        names = _tool_names(svc)
        self.assertNotIn("o_schema_OrderSet", names)


if __name__ == "__main__":
    import sys
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = None
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

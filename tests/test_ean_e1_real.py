#!/usr/bin/env python3
"""Quick check: does the real EAN E1 EDMX suppress create correctly?"""
import sys
sys.path.insert(0, ".")
from unittest.mock import patch, MagicMock
from bridge_core.odata_service import ODataService
from bridge_core.bridge import Bridge as MCPBridge

# Exact real EDMX — both keys (Ean, Type) carry SAP__core.Computed with no Bool attr.
# No explicit InsertRestrictions — suppression must come from the "all keys computed" step.
EDMX = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"'
    '           xmlns="http://docs.oasis-open.org/odata/ns/edm" Version="4.0">'
    '  <edmx:DataServices>'
    '    <Schema Namespace="com.sap.gateway.srvd.zsd_jam_e1_ean_reserve.v0001" Alias="SAP__self">'
    '      <EntityType Name="E1EanReserveType">'
    '        <Key>'
    '          <PropertyRef Name="Ean"/>'
    '          <PropertyRef Name="Type"/>'
    '        </Key>'
    '        <Property Name="Ean"            Type="Edm.String"        Nullable="false" MaxLength="18"/>'
    '        <Property Name="Type"           Type="Edm.String"        Nullable="false" MaxLength="2"/>'
    '        <Property Name="DepartmentCode" Type="Edm.String"        Nullable="false" MaxLength="1"/>'
    '        <Property Name="UserName"       Type="Edm.String"        Nullable="false" MaxLength="12"/>'
    '        <Property Name="CreateTime"     Type="Edm.DateTimeOffset"/>'
    '      </EntityType>'
    '      <Action Name="GenerateEAN_E1" IsBound="true">'
    '        <Parameter Name="_it" Type="Collection(com.sap.gateway.srvd.zsd_jam_e1_ean_reserve.v0001.E1EanReserveType)" Nullable="false"/>'
    '        <Parameter Name="ean_count" Type="Edm.Int32" Nullable="false"/>'
    '      </Action>'
    '      <EntityContainer Name="Container">'
    '        <EntitySet Name="E1EanReserve" EntityType="com.sap.gateway.srvd.zsd_jam_e1_ean_reserve.v0001.E1EanReserveType"/>'
    '      </EntityContainer>'
    '      <Annotations Target="SAP__self.E1EanReserveType/Ean">'
    '        <Annotation Term="SAP__core.Computed"/>'
    '      </Annotations>'
    '      <Annotations Target="SAP__self.E1EanReserveType/Type">'
    '        <Annotation Term="SAP__core.Computed"/>'
    '      </Annotations>'
    '      <Annotations Target="SAP__self.Container/E1EanReserve">'
    '        <Annotation Term="SAP__capabilities.SearchRestrictions">'
    '          <Record><PropertyValue Property="Searchable" Bool="false"/></Record>'
    '        </Annotation>'
    '        <Annotation Term="SAP__capabilities.DeleteRestrictions">'
    '          <Record><PropertyValue Property="Deletable" Path="__EntityControl/Deletable"/></Record>'
    '        </Annotation>'
    '        <Annotation Term="SAP__capabilities.UpdateRestrictions">'
    '          <Record><PropertyValue Property="Updatable" Path="__EntityControl/Updatable"/></Record>'
    '        </Annotation>'
    '      </Annotations>'
    '    </Schema>'
    '  </edmx:DataServices>'
    '</edmx:Edmx>'
).encode()

mock_resp = MagicMock()
mock_resp.content = EDMX
mock_resp.raise_for_status = MagicMock()
mock_resp.headers = {}

with patch('bridge_core.odata_service.requests.Session') as MockSession:
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    MockSession.return_value = mock_sess
    svc = ODataService(
        alias="ean_e1",
        url="http://fake/",
        username="x",
        password="x",
        include_actions=["GenerateEAN_E1"],
    )

es = svc.entity_sets["E1EanReserve"]
print("Keys          :", es["keys"])
print("Ean computed  :", es["props"]["Ean"].get("computed"))
print("Type computed :", es["props"]["Type"].get("computed"))
print("Capabilities  :", es["capabilities"])
print()

bridge = MCPBridge([svc])
names  = sorted(t["name"] for t in bridge._all_tools)
print("Tools generated:")
for n in names:
    print(" ", n)

print()
ok = True
def chk(label, got, expected):
    global ok
    status = "OK" if got == expected else "FAIL"
    if got != expected:
        ok = False
    print(f"  [{status}] {label}: {got}  (expected {expected})")

chk("create suppressed", not any("create" in n for n in names), True)
chk("filter present",        any("filter" in n for n in names), True)
chk("get present",           any(n == "ean_e1_get_E1EanReserve" for n in names), True)
chk("search suppressed",     not any("search" in n for n in names), True)
chk("GenerateEAN_E1 present",any("GenerateEAN_E1" in n for n in names), True)

print()
print("ALL PASS" if ok else "SOME FAILED")

#!/usr/bin/env python3
"""
Test the three v2 backports using the Azure Samples API_BUSINESS_PARTNER.edmx.

Downloads the EDMX from GitHub, creates a mock ODataService that parses it,
then verifies:
  1. $format=json and $inlinecount=allpages are injected for v2 requests
  2. v2 response shape {d:{results:[...]}} is normalised to {value:[...]}
  3. GUID keys are wrapped with guid'...' for v2 services
  4. OData-Version: 4.0 header is NOT sent for v2 services
"""

import json
import re
import sys
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from unittest.mock import patch, MagicMock
from io import BytesIO

# Make bridge_core importable
sys.path.insert(0, ".")
from bridge_core.constants import _GUID_RE
from bridge_core.odata_service import ODataService

EDMX_URL = (
    "https://raw.githubusercontent.com/Azure-Samples/"
    "app-service-javascript-sap-cloud-sdk-quickstart/main/src/api/API_BUSINESS_PARTNER.edmx"
)


def download_edmx() -> bytes:
    """Download the Business Partner EDMX once."""
    req = urllib.request.Request(EDMX_URL)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# -- Shared EDMX --
_edmx_data: bytes = b""


def setUpModule():
    global _edmx_data
    print(f"Downloading EDMX from {EDMX_URL} ...")
    _edmx_data = download_edmx()
    print(f"  Downloaded {len(_edmx_data):,} bytes")


class TestV2Detection(unittest.TestCase):
    """Verify that the EDMX is detected as OData v2."""

    def test_edmx_is_v2(self):
        # Quick sanity check: the downloaded EDMX should use an older EDM namespace
        root = ET.fromstring(_edmx_data)
        # Should NOT contain the OASIS v4 namespace
        v4_ns = "http://docs.oasis-open.org/odata/ns/edm"
        schema_v4 = root.find(f".//{{{v4_ns}}}Schema")
        self.assertIsNone(schema_v4, "EDMX should NOT be OData v4")

        # Should contain a v2 EDM namespace
        v2_nses = [
            "http://schemas.microsoft.com/ado/2008/09/edm",
            "http://schemas.microsoft.com/ado/2007/05/edm",
            "http://schemas.microsoft.com/ado/2006/04/edm",
        ]
        found = any(root.find(f".//{{{ns}}}Schema") is not None for ns in v2_nses)
        self.assertTrue(found, "EDMX should contain a v2 EDM Schema element")


class TestV2ServiceParsing(unittest.TestCase):
    """Create an ODataService from the EDMX and validate metadata parsing."""

    @classmethod
    def setUpClass(cls):
        """Create a service by mocking the HTTP metadata fetch."""
        mock_response = MagicMock()
        mock_response.content = _edmx_data
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}

        with patch('bridge_core.odata_service.requests.Session') as MockSession:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_response
            MockSession.return_value = mock_sess
            cls.svc = ODataService(
                alias="bp",
                url="https://example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER",
                username="test",
                password="test",
            )

    def test_detected_as_v2(self):
        self.assertEqual(self.svc.odata_version, "2",
                         "Service should be detected as OData v2")

    def test_entity_sets_loaded(self):
        self.assertGreater(len(self.svc.entity_sets), 0,
                           "Should have parsed entity sets")
        print(f"  Entity sets: {len(self.svc.entity_sets)}")
        # Business Partner is the main entity set
        bp_names = [n for n in self.svc.entity_sets if "BusinessPartner" in n]
        self.assertTrue(len(bp_names) > 0,
                        f"Should have a BusinessPartner entity set, got: {list(self.svc.entity_sets.keys())[:10]}")
        print(f"  BP-related entity sets: {bp_names[:5]}")

    def test_function_imports_loaded(self):
        # BP EDMX has no function imports — verify it parses cleanly with 0
        print(f"  Functions: {len(self.svc.actions)} (BP EDMX has none)")
        self.assertIsInstance(self.svc.actions, list)

    def test_sap_labels_extracted(self):
        """At least some SAP labels should be extracted from the EDMX."""
        for es_name, es in self.svc.entity_sets.items():
            for pname, pinfo in es["props"].items():
                if pinfo.get("label"):
                    print(f"  Label example: {es_name}.{pname} → '{pinfo['label']}'")
                    return
        self.fail("No SAP labels found — sap:label extraction may be broken")


class TestV2Params(unittest.TestCase):
    """Test that v2 services get $format=json and $inlinecount=allpages."""

    @classmethod
    def setUpClass(cls):
        mock_response = MagicMock()
        mock_response.content = _edmx_data
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}

        with patch('bridge_core.odata_service.requests.Session') as MockSession:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_response
            MockSession.return_value = mock_sess
            cls.svc = ODataService(
                alias="bp",
                url="https://example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER",
                username="test",
                password="test",
            )

    def test_v2_params_helper(self):
        params = self.svc._v2_params()
        self.assertEqual(params["$format"], "json")
        self.assertEqual(params["$inlinecount"], "allpages")

    def test_v4_service_gets_no_extra_params(self):
        # Temporarily set version to 4
        orig = self.svc.odata_version
        self.svc.odata_version = "4"
        try:
            params = self.svc._v2_params()
            self.assertEqual(params, {})
        finally:
            self.svc.odata_version = orig

    def test_filter_url_contains_format_json(self):
        """Capture the URL built by filter() and check v2 params."""
        captured = []

        orig_request = self.svc._request
        def mock_request(method, url, **kwargs):
            captured.append((url, kwargs))
            # Return a fake v2 response
            return {"d": {"results": [{"BusinessPartner": "1"}], "__count": "1"}}

        self.svc._request = mock_request
        try:
            # Pick the first entity set that has BusinessPartner in the name
            es = next(n for n in self.svc.entity_sets if "A_BusinessPartner" == n)
            result = self.svc.filter(es, {})
            self.assertEqual(len(captured), 1)
            url, kwargs = captured[0]
            params = kwargs.get("params", {})
            print(f"  Filter URL: {url}, params: {params}")
            self.assertEqual(params.get("$format"), "json", "params should contain $format=json")
            self.assertEqual(params.get("$inlinecount"), "allpages", "params should contain $inlinecount=allpages")
        finally:
            self.svc._request = orig_request

    def test_filter_normalises_v2_response(self):
        """filter() should normalise v2 {d:{results:[]}} to {value:[]}."""
        def mock_request(method, url, **kwargs):
            return {
                "d": {
                    "results": [
                        {"BusinessPartner": "1", "BusinessPartnerFullName": "John Doe"},
                        {"BusinessPartner": "2", "BusinessPartnerFullName": "Jane Doe"},
                    ],
                    "__count": "42",
                }
            }

        self.svc._request = mock_request
        try:
            es = next(n for n in self.svc.entity_sets if "A_BusinessPartner" == n)
            result = self.svc.filter(es, {"$top": 2})
            self.assertIn("value", result, "Should have 'value' key (v4 style)")
            self.assertEqual(len(result["value"]), 2)
            self.assertEqual(result.get("@odata.count"), 42)
            print(f"  Normalised response: {json.dumps(result, indent=2)[:200]}")
        finally:
            pass

    def test_get_url_contains_format_json(self):
        """get() should also include $format=json for v2."""
        captured = []

        def mock_request(method, url, **kwargs):
            captured.append((url, kwargs))
            return {"d": {"BusinessPartner": "1", "BusinessPartnerFullName": "John"}}

        self.svc._request = mock_request
        try:
            result = self.svc.get("A_BusinessPartner", "BusinessPartner='0000000001'", {})
            url, kwargs = captured[0]
            params = kwargs.get("params", {})
            print(f"  Get URL: {url}, params: {params}")
            self.assertEqual(params.get("$format"), "json", "params should contain $format=json")
        finally:
            pass

    def test_count_v2_uses_inlinecount(self):
        """count() for v2 should use $inlinecount=allpages&$top=0 instead of /$count."""
        captured = []

        def mock_request(method, url, **kwargs):
            captured.append((url, kwargs))
            return {"d": {"results": [], "__count": "100"}}

        self.svc._request = mock_request
        try:
            result = self.svc.count("A_BusinessPartner")
            url, kwargs = captured[0]
            params = kwargs.get("params", {})
            print(f"  Count URL: {url}, params: {params}")
            self.assertNotIn("/$count", url, "v2 should NOT use /$count endpoint")
            self.assertEqual(params.get("$inlinecount"), "allpages")
            self.assertEqual(params.get("$top"), "0")
        finally:
            pass


class TestV2Headers(unittest.TestCase):
    """Test that v2 services don't send OData-Version: 4.0."""

    @classmethod
    def setUpClass(cls):
        mock_response = MagicMock()
        mock_response.content = _edmx_data
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}

        with patch('bridge_core.odata_service.requests.Session') as MockSession:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_response
            MockSession.return_value = mock_sess
            cls.svc = ODataService(
                alias="bp",
                url="https://example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER",
                username="test",
                password="test",
            )

    def test_v2_request_no_odata_version_header(self):
        """For v2, Accept should be application/json without OData-Version: 4.0."""
        fake_resp = MagicMock()
        fake_resp.content = b'{"d":{"results":[]}}'
        fake_resp.raise_for_status = MagicMock()

        orig_session = self.svc._bootstrap_session
        mock_sess = MagicMock()
        mock_sess.request.return_value = fake_resp
        self.svc._bootstrap_session = mock_sess
        try:
            self.svc._request("GET", "https://example.com/test")
            call_headers = mock_sess.request.call_args[1].get("headers", {})
            self.assertNotIn("OData-Version", call_headers,
                             "v2 should NOT send OData-Version header")
            self.assertEqual(call_headers.get("Accept"), "application/json")
            print(f"  v2 request headers: {call_headers}")
        finally:
            self.svc._bootstrap_session = orig_session


class TestGUIDWrapping(unittest.TestCase):
    """Test GUID auto-wrapping for v2 services."""

    @classmethod
    def setUpClass(cls):
        mock_response = MagicMock()
        mock_response.content = _edmx_data
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}

        with patch('bridge_core.odata_service.requests.Session') as MockSession:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_response
            MockSession.return_value = mock_sess
            cls.svc = ODataService(
                alias="bp",
                url="https://example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER",
                username="test",
                password="test",
            )

    def test_guid_regex(self):
        self.assertTrue(_GUID_RE.match("069f2c5e-2738-1eeb-b7bd-cd0f34d2052d"))
        self.assertFalse(_GUID_RE.match("not-a-guid"))
        self.assertFalse(_GUID_RE.match("12345"))

    def test_wrap_guid_key_by_edm_type(self):
        """If the property is typed Edm.Guid, wrap with guid'...'."""
        # Create a fake entity set with a Guid key
        self.svc.entity_sets["_TestGuid"] = {
            "keys": ["Id"],
            "props": {
                "Id": {"type": "string", "edm_type": "Edm.Guid", "nullable": False, "label": "", "internal": False}
            },
            "nav_props": [],
            "capabilities": {},
        }
        result = self.svc._wrap_guid_key("_TestGuid", "Id", "069f2c5e-2738-1eeb-b7bd-cd0f34d2052d")
        self.assertEqual(result, "Id=guid'069f2c5e-2738-1eeb-b7bd-cd0f34d2052d'")
        print(f"  GUID by Edm.Guid type: {result}")
        del self.svc.entity_sets["_TestGuid"]

    def test_wrap_guid_key_by_pattern(self):
        """Even without Edm.Guid type, GUID-shaped values get wrapped for v2."""
        # Create a fake entity set with a string key
        self.svc.entity_sets["_TestGuid2"] = {
            "keys": ["Id"],
            "props": {
                "Id": {"type": "string", "edm_type": "Edm.String", "nullable": False, "label": "", "internal": False}
            },
            "nav_props": [],
            "capabilities": {},
        }
        result = self.svc._wrap_guid_key("_TestGuid2", "Id", "069f2c5e-2738-1eeb-b7bd-cd0f34d2052d")
        self.assertEqual(result, "Id=guid'069f2c5e-2738-1eeb-b7bd-cd0f34d2052d'")
        print(f"  GUID by pattern: {result}")
        del self.svc.entity_sets["_TestGuid2"]

    def test_non_guid_not_wrapped(self):
        """Regular string keys should NOT be wrapped with guid'...'."""
        self.svc.entity_sets["_TestStr"] = {
            "keys": ["Code"],
            "props": {
                "Code": {"type": "string", "edm_type": "Edm.String", "nullable": False, "label": "", "internal": False}
            },
            "nav_props": [],
            "capabilities": {},
        }
        result = self.svc._wrap_guid_key("_TestStr", "Code", "ABC123")
        self.assertEqual(result, "Code='ABC123'")
        print(f"  Non-GUID: {result}")
        del self.svc.entity_sets["_TestStr"]

    def test_integer_key_not_wrapped(self):
        """Integer keys should use bare value."""
        self.svc.entity_sets["_TestInt"] = {
            "keys": ["Id"],
            "props": {
                "Id": {"type": "integer", "edm_type": "Edm.Int32", "nullable": False, "label": "", "internal": False}
            },
            "nav_props": [],
            "capabilities": {},
        }
        result = self.svc._wrap_guid_key("_TestInt", "Id", 42)
        self.assertEqual(result, "Id=42")
        print(f"  Integer key: {result}")
        del self.svc.entity_sets["_TestInt"]

    def test_v4_guid_not_wrapped(self):
        """v4 services should NOT wrap GUIDs with guid'...'."""
        orig = self.svc.odata_version
        self.svc.odata_version = "4"
        try:
            self.svc.entity_sets["_TestV4"] = {
                "keys": ["Id"],
                "props": {
                    "Id": {"type": "string", "edm_type": "Edm.Guid", "nullable": False, "label": "", "internal": False}
                },
                "nav_props": [],
                "capabilities": {},
            }
            result = self.svc._wrap_guid_key("_TestV4", "Id", "069f2c5e-2738-1eeb-b7bd-cd0f34d2052d")
            self.assertEqual(result, "Id='069f2c5e-2738-1eeb-b7bd-cd0f34d2052d'")
            print(f"  v4 GUID (no wrapping): {result}")
            del self.svc.entity_sets["_TestV4"]
        finally:
            self.svc.odata_version = orig


class TestV2ResponseNormalisation(unittest.TestCase):
    """Test _normalize_v2_response static method."""

    def test_collection_response(self):
        v2 = {"d": {"results": [{"A": 1}, {"A": 2}], "__count": "42"}}
        result = ODataService._normalize_v2_response(v2)
        self.assertEqual(result["value"], [{"A": 1}, {"A": 2}])
        self.assertEqual(result["@odata.count"], 42)

    def test_collection_with_next_link(self):
        v2 = {"d": {"results": [{"A": 1}], "__next": "http://host/next?skip=1"}}
        result = ODataService._normalize_v2_response(v2)
        self.assertEqual(result["@odata.nextLink"], "http://host/next?skip=1")

    def test_single_entity_response(self):
        v2 = {"d": {"BusinessPartner": "1", "Name": "John"}}
        result = ODataService._normalize_v2_response(v2)
        self.assertEqual(result, {"BusinessPartner": "1", "Name": "John"})

    def test_v4_response_passthrough(self):
        v4 = {"value": [{"A": 1}]}
        result = ODataService._normalize_v2_response(v4)
        # No "d" key, should pass through unchanged
        self.assertEqual(result, v4)

    def test_empty_collection(self):
        v2 = {"d": {"results": [], "__count": "0"}}
        result = ODataService._normalize_v2_response(v2)
        self.assertEqual(result["value"], [])
        self.assertEqual(result["@odata.count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

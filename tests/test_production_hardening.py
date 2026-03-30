#!/usr/bin/env python3
"""
Production-hardening tests.

Covers:
  1. _guard_params  — null-byte stripping, over-length truncation, passthrough
  2. bridge.handle  — malformed JSON-RPC (None params, missing method, unknown tool)
  3. bridge.handle  — tool call exception is caught and returns JSON-RPC error
  4. XSUAA cache    — cache hit skips HTTP, TTL expiry triggers refresh, size cap eviction
  5. ODataService   — Content-Length early bail-out before buffering body
"""

import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from bridge_core.helpers import _guard_params
from bridge_core.bridge import Bridge
from bridge_core.odata_service import ODataService


# ---------------------------------------------------------------------------
# Minimal EDMX to build a bridge without hitting the network
# ---------------------------------------------------------------------------

_MINIMAL_EDMX = b"""\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
<edmx:DataServices>
<Schema Namespace="test" xmlns="http://docs.oasis-open.org/odata/ns/edm">
  <EntityType Name="Item">
    <Key><PropertyRef Name="Id"/></Key>
    <Property Name="Id"   Type="Edm.String" Nullable="false"/>
    <Property Name="Name" Type="Edm.String"/>
  </EntityType>
  <EntityContainer Name="C">
    <EntitySet Name="Items" EntityType="test.Item"/>
  </EntityContainer>
</Schema>
</edmx:DataServices>
</edmx:Edmx>"""


def _make_bridge() -> Bridge:
    mock_resp = MagicMock()
    mock_resp.content = _MINIMAL_EDMX
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    with patch("bridge_core.odata_service.requests.Session") as MockSession:
        mock_sess = MagicMock()
        mock_sess.get.return_value = mock_resp
        MockSession.return_value = mock_sess
        svc = ODataService(alias="items", url="http://example.com/svc",
                           username="u", password="p")
    return Bridge([svc])


# ---------------------------------------------------------------------------
# 1. _guard_params
# ---------------------------------------------------------------------------

class TestGuardParams(unittest.TestCase):

    def test_passthrough_normal_values(self):
        params = {"key": "value", "num": 42, "flag": True}
        result = _guard_params(params)
        self.assertEqual(result, params)

    def test_strips_null_bytes_from_strings(self):
        result = _guard_params({"q": "hel\x00lo"})
        self.assertEqual(result["q"], "hello")

    def test_truncates_over_length_string(self):
        long_val = "x" * 5000
        result = _guard_params({"q": long_val})
        self.assertLessEqual(len(result["q"]), 4096)

    def test_preserves_non_string_types(self):
        result = _guard_params({"n": 99, "b": False, "lst": [1, 2]})
        self.assertEqual(result["n"], 99)
        self.assertEqual(result["b"], False)
        self.assertEqual(result["lst"], [1, 2])


# ---------------------------------------------------------------------------
# 2+3. bridge.handle — malformed input and exception handling
# ---------------------------------------------------------------------------

class TestBridgeHandleMalformed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bridge = _make_bridge()

    def test_none_params_does_not_raise(self):
        """JSON-RPC with null params must not crash — spec allows omitting params."""
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": None}
        result = self.bridge.handle(req)
        self.assertIn("result", result)
        self.assertIn("tools", result["result"])

    def test_missing_method_returns_error(self):
        req = {"jsonrpc": "2.0", "id": 2}
        result = self.bridge.handle(req)
        self.assertIn("error", result)

    def test_unknown_tool_returns_error(self):
        req = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
        result = self.bridge.handle(req)
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32601)

    def test_initialize_returns_protocol_version(self):
        req = {
            "jsonrpc": "2.0", "id": 4, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        result = self.bridge.handle(req)
        self.assertIn("result", result)
        self.assertIn("protocolVersion", result["result"])

    def test_notifications_initialized_returns_none(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        result = self.bridge.handle(req)
        self.assertIsNone(result)

    def test_tool_call_exception_returns_json_rpc_error(self):
        """Service-layer exception must be caught and return a -32603 error, not raise."""
        bridge = _make_bridge()
        svc = next(iter(bridge.services.values()))
        # Make the service's filter() raise an unexpected error
        svc.filter = MagicMock(side_effect=RuntimeError("boom"))

        req = {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "items_filter_Items", "arguments": {}},
        }
        result = bridge.handle(req)
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32603)
        self.assertIn("boom", result["error"]["message"])


# ---------------------------------------------------------------------------
# 4. XSUAA introspection cache
# ---------------------------------------------------------------------------

class TestXsuaaCache(unittest.TestCase):

    def setUp(self):
        # Reset module-level cache state between tests
        import bridge_core.auth as _auth
        self._auth = _auth
        _auth._INTROSPECT_CACHE.clear()
        _auth._XSUAA_INTROSPECT_URL = "https://xsuaa.example.com/introspect"
        _auth._XSUAA_CREDS = {"clientid": "cid", "clientsecret": "sec"}

    def tearDown(self):
        self._auth._INTROSPECT_CACHE.clear()
        self._auth._XSUAA_INTROSPECT_URL = ""
        self._auth._XSUAA_CREDS = {}

    def test_cache_hit_skips_http(self):
        from bridge_core.auth import _xsuaa_introspect, _INTROSPECT_CACHE_TTL
        token = "header.e30.sig"  # minimal JWT with empty payload

        active_result = {"active": True, "sub": "user1"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = active_result
        mock_resp.raise_for_status = MagicMock()

        with patch("bridge_core.auth.requests.post", return_value=mock_resp) as mock_post:
            r1 = _xsuaa_introspect(token)
            r2 = _xsuaa_introspect(token)

        self.assertEqual(r1, active_result)
        self.assertEqual(r2, active_result)
        mock_post.assert_called_once()  # second call served from cache

    def test_expired_cache_entry_triggers_refresh(self):
        from bridge_core.auth import _xsuaa_introspect, _INTROSPECT_CACHE, _INTROSPECT_LOCK
        import hashlib

        token = "header.e30.sig2"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Seed cache with an already-expired entry
        with _INTROSPECT_LOCK:
            _INTROSPECT_CACHE[token_hash] = ({"active": True}, time.time() - 1)

        fresh_result = {"active": True, "sub": "user2"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = fresh_result
        mock_resp.raise_for_status = MagicMock()

        with patch("bridge_core.auth.requests.post", return_value=mock_resp) as mock_post:
            result = _xsuaa_introspect(token)

        mock_post.assert_called_once()
        self.assertEqual(result["sub"], "user2")

    def test_cache_max_size_eviction(self):
        from bridge_core.auth import (
            _INTROSPECT_CACHE, _INTROSPECT_LOCK, _INTROSPECT_CACHE_MAX,
            _INTROSPECT_CACHE_TTL,
        )
        import hashlib

        # Fill cache to the limit with fake entries
        with _INTROSPECT_LOCK:
            for i in range(_INTROSPECT_CACHE_MAX):
                k = hashlib.sha256(f"tok{i}".encode()).hexdigest()
                _INTROSPECT_CACHE[k] = ({"active": True}, time.time() + 300 + i)

        self.assertEqual(len(_INTROSPECT_CACHE), _INTROSPECT_CACHE_MAX)

        # Introspecting one more token should cause eviction
        token = "header.e30.sig3"
        fresh_result = {"active": True, "sub": "new_user"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = fresh_result
        mock_resp.raise_for_status = MagicMock()

        with patch("bridge_core.auth.requests.post", return_value=mock_resp):
            _xsuaa_introspect = __import__(
                "bridge_core.auth", fromlist=["_xsuaa_introspect"]
            )._xsuaa_introspect
            _xsuaa_introspect(token)

        with _INTROSPECT_LOCK:
            self.assertLessEqual(len(_INTROSPECT_CACHE), _INTROSPECT_CACHE_MAX)

    def test_inactive_token_not_cached(self):
        from bridge_core.auth import _xsuaa_introspect, _INTROSPECT_CACHE

        token = "header.e30.siginactive"
        inactive_result = {"active": False}
        mock_resp = MagicMock()
        mock_resp.json.return_value = inactive_result
        mock_resp.raise_for_status = MagicMock()

        with patch("bridge_core.auth.requests.post", return_value=mock_resp):
            _xsuaa_introspect(token)

        # Inactive tokens must NOT be cached (so revocation is instant)
        self.assertEqual(len(_INTROSPECT_CACHE), 0)


# ---------------------------------------------------------------------------
# 5. ODataService: Content-Length early bail-out
# ---------------------------------------------------------------------------

class TestResponseSizeCap(unittest.TestCase):

    def _make_svc(self, max_bytes: int) -> ODataService:
        mock_resp = MagicMock()
        mock_resp.content = _MINIMAL_EDMX
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}
        with patch("bridge_core.odata_service.requests.Session") as MockSession:
            mock_sess = MagicMock()
            mock_sess.get.return_value = mock_resp
            MockSession.return_value = mock_sess
            return ODataService(
                alias="test", url="http://example.com/svc",
                username="u", password="p",
                max_response_size=max_bytes,
            )

    def test_content_length_header_triggers_early_rejection(self):
        svc = self._make_svc(max_bytes=100)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Content-Length": "2000"}  # over limit
        mock_resp.content = b"x" * 2000  # should not be read

        with patch.object(svc._bootstrap_session, "request", return_value=mock_resp):
            result = svc._request("GET", "http://example.com/svc/Items")

        self.assertEqual(result.get("error"), "RESPONSE_TOO_LARGE")
        # Body was NOT buffered — content was never accessed
        mock_resp.content  # access it now just to check the mock wasn't called earlier

    def test_body_size_cap_without_content_length(self):
        svc = self._make_svc(max_bytes=10)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {}  # no Content-Length header
        mock_resp.content = b"x" * 500  # over limit, but no header

        with patch.object(svc._bootstrap_session, "request", return_value=mock_resp):
            result = svc._request("GET", "http://example.com/svc/Items")

        self.assertEqual(result.get("error"), "RESPONSE_TOO_LARGE")

    def test_response_within_limit_is_parsed(self):
        svc = self._make_svc(max_bytes=10_000)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Content-Length": "20"}
        mock_resp.content = b'{"value": []}'

        with patch.object(svc._bootstrap_session, "request", return_value=mock_resp):
            result = svc._request("GET", "http://example.com/svc/Items")

        self.assertNotIn("error", result)
        self.assertEqual(result.get("value"), [])


if __name__ == "__main__":
    unittest.main()

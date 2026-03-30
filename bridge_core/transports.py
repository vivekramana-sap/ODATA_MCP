"""
Transport layer for the OData MCP Bridge.

Provides the Streamable HTTP transport (MCP over HTTP/JSON-RPC + CORS),
a trace mode, and the ThreadingHTTPServer used by server.py.
"""

import base64
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import auth as _auth
from .auth import _xsuaa_introspect, _xsuaa_oauth_metadata
from .bridge import Bridge


# ---------------------------------------------------------------------------
# Threading HTTP Server
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request HTTP server used to host the MCP endpoint."""
    daemon_threads = True


# ---------------------------------------------------------------------------
# Trace mode — print all registered tools and exit
# ---------------------------------------------------------------------------

def print_trace(bridge: Bridge) -> None:
    sep = "=" * 80
    print(sep)
    print("OData MCP Bridge -- Trace Mode")
    print(sep)
    summary = {
        "services": [
            {
                "alias":       alias,
                "url":         svc.url,
                "entity_sets": list(svc.entity_sets.keys()),
                "actions":     [a["name"] for a in svc.actions],
            }
            for alias, svc in bridge.services.items()
        ],
        "total_tools": len(bridge._all_tools),
        "tools":       bridge._all_tools,
    }
    print(json.dumps(summary, indent=2))
    print(sep)
    print(f"Total tools registered: {len(bridge._all_tools)}")
    print("Remove --trace to start the actual MCP server.")
    print(sep)


# ---------------------------------------------------------------------------
# HTTP transport  (MCP over HTTP/JSON-RPC + CORS)
# ---------------------------------------------------------------------------

def make_http_handler(
    bridges,
    mcp_token:   str = "",
    passthrough: bool = False,
):
    # Normalise: accept a single Bridge for backward compat
    if isinstance(bridges, Bridge):
        _bridges: dict = {"": bridges}
    else:
        _bridges: dict = bridges  # dict[str, Bridge]

    _mcp_token   = mcp_token
    _passthrough = passthrough

    def _resolve_bridge(path: str):
        """Return (group, bridge) for a given request path, or (None, None) if not a valid /mcp path."""
        clean = path.split("?")[0].rstrip("/")
        if clean in ("/mcp", "", "/"):
            return "", _bridges.get("", next(iter(_bridges.values())))
        if clean.startswith("/mcp/"):
            group = clean[5:]  # strip leading "/mcp/"
            if not group or "/" in group:
                return None, None
            b = _bridges.get(group)
            return (group, b) if b else (group, None)
        return None, None

    def _discovery(group: str, bridge: Bridge) -> dict:
        return {
            "endpoint":         group or "default",
            "path":             f"/mcp/{group}" if group else "/mcp",
            "protocol":         "mcp",
            "transport":        "Streamable HTTP — POST JSON-RPC to this URL",
            "services":         list(bridge.services.keys()),
            "tools_count":      len(bridge._all_tools),
            "available_groups": sorted(k for k in _bridges if k),
        }

    class MCPHandler(BaseHTTPRequestHandler):
        protocol_version  = "HTTP/1.1"
        mcp_username: str = ""
        mcp_password: str = ""

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[bridge] {self.address_string()} {fmt % args}\n")

        def _cors(self) -> None:
            origin = self.headers.get("Origin", "")
            allowed = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
            if allowed:
                allowed_set = {o.strip() for o in allowed.split(",") if o.strip()}
                if origin in allowed_set:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
            else:
                # Local dev fallback: allow all
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type,Authorization,Accept",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        @staticmethod
        def _decode_basic(ah: str):
            """Decode a Basic auth header; returns (user, password) or (None, None)."""
            try:
                u, _, p = base64.b64decode(ah[6:]).decode().partition(":")
                return u, p
            except Exception:
                return None, None

        def _auth_ok(self) -> bool:
            ah = self.headers.get("Authorization", "")

            if _auth._XSUAA_INTROSPECT_URL:
                if not ah.startswith("Bearer "):
                    return False
                result = _xsuaa_introspect(ah[7:].strip())
                if result.get("active"):
                    self._xsuaa_user = result.get("user_name") or result.get("sub", "")
                    return True
                return False

            if _mcp_token:
                if ah == f"Bearer {_mcp_token.strip()}":
                    return True
                if ah.startswith("Basic ") and MCPHandler.mcp_username:
                    u, p = MCPHandler._decode_basic(ah)
                    return u == MCPHandler.mcp_username and p == MCPHandler.mcp_password
                return False

            if MCPHandler.mcp_username:
                if ah.startswith("Basic "):
                    u, p = MCPHandler._decode_basic(ah)
                    return u == MCPHandler.mcp_username and p == MCPHandler.mcp_password
                return False

            return True

        def _send_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path in ("/health", "/healthz"):
                all_svcs = [alias for b in _bridges.values() for alias in b.services]
                self._send_json({"status": "ok", "services": all_svcs})
            elif self.path == "/mcp" or (
                self.path.startswith("/mcp/")
                and "/" not in self.path[5:].split("?")[0]
                and self.path[5:].split("?")[0]
            ):
                group, bridge = _resolve_bridge(self.path.split("?")[0])
                if bridge is None:
                    self._send_json({"error": f"No MCP endpoint at {self.path.split('?')[0]}"}, status=404)
                else:
                    self._send_json(_discovery(group, bridge), indent=2)
            elif self.path in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                if not _auth._XSUAA_INTROSPECT_URL:
                    self._send_empty(404)
                    return
                host = self.headers.get("Host", "localhost")
                self._send_json(_xsuaa_oauth_metadata(bridge_origin=f"https://{host}"))
            elif self.path.startswith("/authorize"):
                if not _auth._XSUAA_CREDS:
                    self._send_empty(404)
                    return
                base     = _auth._XSUAA_CREDS.get("url", "").rstrip("/")
                qs       = self.path.split("?", 1)[1] if "?" in self.path else ""
                idp_hint = os.environ.get("IDP_HINT", "").strip()
                if idp_hint:
                    sep = "&" if qs else ""
                    qs  = qs + sep + "idp=" + urllib.parse.quote(idp_hint)
                location = base + "/oauth/authorize?" + qs
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
            else:
                self._send_empty(404)

        def do_POST(self):
            if self.path == "/register":
                if not _auth._XSUAA_CREDS:
                    self._send_empty(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    req_data = json.loads(self.rfile.read(length)) if length else {}
                except Exception:
                    req_data = {}
                self._send_json({
                    "client_id":                  _auth._XSUAA_CREDS.get("clientid", ""),
                    "client_secret":              _auth._XSUAA_CREDS.get("clientsecret", ""),
                    "grant_types":                ["authorization_code", "client_credentials"],
                    "token_endpoint_auth_method": "client_secret_basic",
                    "redirect_uris":              req_data.get("redirect_uris", []),
                    "scope":                      "openid",
                }, status=201)
                return

            # ---- Route /mcp and /mcp/<group> paths ----
            group, active_bridge = _resolve_bridge(self.path)
            if group is None:
                self._send_empty(404)
                return

            if active_bridge is None:
                self._send_json({"error": f"Unknown MCP group: {group}"}, status=404)
                return

            if not self._auth_ok():
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length:
                        self.rfile.read(length)
                except Exception:
                    pass
                self.send_response(401)
                self.send_header("Content-Length", "0")
                if _auth._XSUAA_INTROSPECT_URL:
                    host = self.headers.get("Host", "localhost")
                    meta = f"https://{host}/.well-known/oauth-authorization-server"
                    self.send_header(
                        "WWW-Authenticate",
                        f'Bearer realm="mcp", resource_metadata_endpoint="{meta}"',
                    )
                else:
                    self.send_header("WWW-Authenticate", 'Bearer realm="mcp"')
                self._cors()
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).strip()

            if not raw:
                self._send_json(_discovery(group, active_bridge))
                return

            try:
                req = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._send_json({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": f"Parse error: {exc}"}})
                return

            resp = active_bridge.handle(req, auth_header=self._caller_auth())
            if resp is not None:
                self._send_json(resp)
            else:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

        def _caller_auth(self) -> str:
            ah = self.headers.get("Authorization", "")
            # XSUAA mode: token already validated — forward for SAP principal propagation
            if _auth._XSUAA_INTROSPECT_URL and ah.startswith("Bearer "):
                return ah
            # Passthrough mode: forward any auth header (Basic or Bearer)
            if _passthrough and ah:
                return ah
            return ""

        def _send_json(self, data: dict, status: int = 200, indent=None) -> None:
            # MCP Streamable HTTP (2025-03-26): respond as SSE when client advertises it.
            body = json.dumps(data, indent=indent).encode()
            if "text/event-stream" in self.headers.get("Accept", ""):
                body = b"data: " + body + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type",  "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

    return MCPHandler

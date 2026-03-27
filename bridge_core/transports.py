"""
Transport layer for the OData MCP Bridge.

Includes: stdio transport, HTTP transport with CORS/auth, and trace mode.
"""

import base64
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import auth as _auth
from .auth import _xsuaa_introspect, _xsuaa_oauth_metadata
from .bridge import Bridge


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
# stdio transport  (Claude Desktop / Claude Code / any MCP host)
# ---------------------------------------------------------------------------

def run_stdio(bridge: Bridge, verbose: bool = False) -> None:
    """Read JSON-RPC requests from stdin and write responses to stdout."""
    if verbose:
        sys.stderr.write("[bridge] transport: stdio\n")

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            resp = {
                "jsonrpc": "2.0",
                "id":      None,
                "error":   {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = bridge.handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


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

        def _auth_ok(self) -> bool:
            ah = self.headers.get("Authorization", "")

            if _auth._XSUAA_INTROSPECT_URL:
                if ah.startswith("Bearer "):
                    token  = ah[7:].strip()
                    result = _xsuaa_introspect(token)
                    if result.get("active"):
                        self._xsuaa_user = (
                            result.get("user_name")
                            or result.get("sub", "")
                        )
                        return True
                    return False
                return False

            if _mcp_token:
                if ah == f"Bearer {_mcp_token.strip()}":
                    return True
                if ah.startswith("Basic ") and MCPHandler.mcp_username:
                    try:
                        decoded = base64.b64decode(ah[6:]).decode()
                        u, _, p = decoded.partition(":")
                        if u == MCPHandler.mcp_username and p == MCPHandler.mcp_password:
                            return True
                    except Exception:
                        pass
                return False

            if MCPHandler.mcp_username:
                if ah.startswith("Basic "):
                    try:
                        decoded = base64.b64decode(ah[6:]).decode()
                        u, _, p = decoded.partition(":")
                        return (
                            u == MCPHandler.mcp_username
                            and p == MCPHandler.mcp_password
                        )
                    except Exception:
                        pass
                return False

            return True

        def _caller_auth(self) -> str:
            if _passthrough:
                ah = self.headers.get("Authorization", "")
                if ah.startswith("Basic "):
                    return ah
            return ""

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path in ("/health", "/healthz"):
                body = json.dumps({"status": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/mcp" or (
                self.path.startswith("/mcp/")
                and "/" not in self.path[5:].split("?")[0]
                and self.path[5:].split("?")[0]
            ):
                self.send_response(405)
                self.send_header("Allow",          "POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
            elif self.path in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                if not _auth._XSUAA_INTROSPECT_URL:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                host   = self.headers.get("Host", "localhost")
                origin = f"https://{host}"
                body   = json.dumps(_xsuaa_oauth_metadata(bridge_origin=origin)).encode()
                self.send_response(200)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/authorize"):
                if not _auth._XSUAA_CREDS:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
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
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_POST(self):
            if self.path == "/register":
                if not _auth._XSUAA_CREDS:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    req_data = json.loads(self.rfile.read(length)) if length else {}
                except Exception:
                    req_data = {}
                redirect_uris = req_data.get("redirect_uris", [])
                body = json.dumps({
                    "client_id":                  _auth._XSUAA_CREDS.get("clientid", ""),
                    "client_secret":              _auth._XSUAA_CREDS.get("clientsecret", ""),
                    "grant_types":                ["authorization_code", "client_credentials"],
                    "token_endpoint_auth_method": "client_secret_basic",
                    "redirect_uris":              redirect_uris,
                    "scope":                      "openid",
                }).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return

            # ---- Route /mcp and /mcp/<group> paths ----
            group, active_bridge = _resolve_bridge(self.path)
            if group is None:
                # Not a recognised MCP path
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if active_bridge is None:
                # Valid /mcp/<group> pattern but no bridge registered for that group
                body = json.dumps({"error": f"Unknown MCP group: {group}"}).encode()
                self.send_response(404)
                self.send_header("Content-Type",   "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
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

            try:
                req = json.loads(raw)
            except json.JSONDecodeError as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id":      None,
                    "error":   {"code": -32700, "message": f"Parse error: {exc}"},
                }
                self._send_json(resp)
                return

            resp = active_bridge.handle(req, auth_header=self._caller_auth())
            if resp is not None:
                self._send_json(resp)
            else:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

        def _send_json(self, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

    return MCPHandler

#!/usr/bin/env python3
"""
JAM OData MCP Bridge â€” Production Patches v3.0
===============================================
Drop these additions into your existing server.py (JAM v2.0).
Each section is clearly marked with WHERE to insert it.

PATCH LIST:
  [P01] Version constant + .env loader
  [P02] Structured logging (replaces sys.stderr.write)
  [P03] MCP wire-trace logger (--trace-mcp)
  [P04] Input guard: string param length cap
  [P05] HTTP retry with exponential backoff
  [P06] Response size cap (--max-response-size)
  [P07] CSRF token manager (thread-safe, retry-on-403)
  [P08] Thread-safe service registry
  [P09] odata_service_info MCP tool
  [P10] /health endpoint
  [P11] Localhost-only guard for HTTP transport
  [P12] Startup banner
  [P13] New argparse arguments
  [P14] Graceful shutdown improvements
"""

import base64
import datetime
import json
import logging
import logging.handlers
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P01] VERSION + .ENV LOADER
# INSERT: at the top of server.py, after all imports
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

__version__ = "3.0.0"

def _load_dotenv(path: str = ".env") -> None:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.
    - Skips blank lines and # comments
    - Strips surrounding quotes from values
    - Does NOT override already-set env vars (safe for Docker / CF)
    Example .env:
        ODATA_USERNAME=sap_user
        ODATA_PASSWORD=secret123
        MCP_TOKEN=mymcpsecret
    """
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip(""'")
        if key and key not in os.environ:
            os.environ[key] = val


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P02] STRUCTURED LOGGING
# INSERT: after _load_dotenv, before _init_btp_proxy
# REPLACE all sys.stderr.write("[bridge] ...") with _log.info/warning/error(...)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _setup_logging(log_file: str | None = None, verbose: bool = False) -> None:
    """
    Configure root logger with:
    - stderr stream handler (always on)
    - optional RotatingFileHandler (10 MB / 3 backups)
    Call this BEFORE _load_dotenv / argparse in main().
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)-8s] %(name)s â€” %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        rh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handlers.append(rh)
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)

_log = logging.getLogger("bridge")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P03] MCP WIRE-TRACE LOGGER
# INSERT: after logging setup
# USE: call _mcp_trace(">>", request_obj) and _mcp_trace("<<", response_obj)
#      in your stdio/HTTP MCP message dispatch loop
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_mcp_trace_fh = None

def _open_mcp_trace() -> None:
    """Open a temp file for MCP wire logging. Call if --trace-mcp is set."""
    global _mcp_trace_fh
    fd, path = tempfile.mkstemp(prefix="mcp_trace_", suffix=".log")
    _mcp_trace_fh = os.fdopen(fd, "w", encoding="utf-8")
    _log.info("MCP trace log: %s", path)

def _mcp_trace(direction: str, obj: dict) -> None:
    """Log a single MCP message. direction is '>>' (in) or '<<' (out)."""
    if _mcp_trace_fh:
        ts = datetime.datetime.utcnow().isoformat()
        _mcp_trace_fh.write(f"{ts} {direction} {json.dumps(obj)}
")
        _mcp_trace_fh.flush()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P04] INPUT GUARD â€” STRING PARAM LENGTH CAP
# INSERT: in your MCP tool call handler, before passing args to OData requests
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

MAX_STRING_PARAM = 4096   # chars; prevents oversized filter/search injections

def _guard_params(params: dict) -> dict:
    """
    Sanitise incoming MCP tool parameters:
    - Truncate strings > MAX_STRING_PARAM chars
    - Strip null bytes
    Returns cleaned copy.
    """
    out = {}
    for k, v in params.items():
        if isinstance(v, str):
            v = v.replace("�", "")          # strip null bytes
            if len(v) > MAX_STRING_PARAM:
                _log.warning("Param '%s' truncated (%d â†’ %d chars)", k, len(v), MAX_STRING_PARAM)
                v = v[:MAX_STRING_PARAM]
        out[k] = v
    return out


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P05] HTTP RETRY WITH EXPONENTIAL BACKOFF
# INSERT: replace direct urllib.request.urlopen() calls in OData fetch helpers
# USAGE: raw = _http_with_retry(req)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _http_with_retry(req: urllib.request.Request,
                     retries: int = 3,
                     backoff: float = 1.0) -> bytes:
    """
    Execute an HTTP request with exponential backoff on transient 5xx errors.
    - 4xx errors (auth, not found) are raised immediately without retry
    - ConnectionError / URLError are retried with backoff
    Returns response body as bytes.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code and exc.code < 500:
                raise          # 4xx â€” not transient, re-raise immediately
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc
        wait = backoff * (2 ** attempt)
        _log.warning("HTTP attempt %d/%d failed (%s), retrying in %.1fs",
                     attempt + 1, retries, last_exc, wait)
        time.sleep(wait)
    raise last_exc


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P06] RESPONSE SIZE CAP
# INSERT: after receiving raw OData response bytes, before JSON parsing
# USAGE: raw = _cap_response(raw, args.max_response_size)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _cap_response(raw: bytes, max_bytes: int) -> bytes:
    """
    Hard byte cap on OData response body.
    If the response exceeds max_bytes, returns a structured JSON error
    with actionable hints instead of a truncated/corrupt payload.
    Default Go equivalent: 5 MB (5_242_880 bytes).
    """
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return raw
    _log.warning("Response capped: %d bytes > limit %d bytes", len(raw), max_bytes)
    return json.dumps({
        "error": {
            "code":    "RESPONSE_TOO_LARGE",
            "message": (
                f"OData response ({len(raw):,} B) exceeds --max-response-size "
                f"({max_bytes:,} B). Narrow your query."
            ),
            "hints": [
                "Add $top=20 to limit rows",
                "Add $select=Field1,Field2 to reduce columns",
                "Add $filter=... to restrict result set",
                "Use the filter_<EntitySet> tool instead of search",
            ],
        }
    }).encode()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P07] CSRF TOKEN MANAGER (thread-safe, auto-retry on 403)
# INSERT: after constants, before service loading
# REPLACE: any inline x-csrf-token logic with _csrf.get() / _csrf.fetch()
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class _CsrfManager:
    """
    Per-service CSRF token cache.
    - fetch()     : calls $metadata with x-csrf-token: Fetch
    - get()       : returns cached token or None
    - invalidate(): clears cached token (call on 403 to force re-fetch)
    Thread-safe via RLock.
    """
    def __init__(self):
        self._tokens: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, service_url: str) -> str | None:
        with self._lock:
            return self._tokens.get(service_url)

    def fetch(self, service_url: str, auth_headers: dict) -> str | None:
        meta_url = service_url.rstrip("/") + "/$metadata"
        headers  = {**auth_headers, "x-csrf-token": "Fetch", "Accept": "application/xml"}
        try:
            req = urllib.request.Request(meta_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                token = r.headers.get("x-csrf-token", "")
            if token:
                with self._lock:
                    self._tokens[service_url] = token
                _log.debug("CSRF token fetched for %s", service_url)
                return token
        except Exception as exc:
            _log.warning("CSRF fetch failed for %s: %s", service_url, exc)
        return None

    def invalidate(self, service_url: str) -> None:
        with self._lock:
            self._tokens.pop(service_url, None)
        _log.debug("CSRF token invalidated for %s", service_url)

    def get_or_fetch(self, service_url: str, auth_headers: dict) -> str | None:
        """Return cached token or fetch a fresh one."""
        token = self.get(service_url)
        if not token:
            token = self.fetch(service_url, auth_headers)
        return token

_csrf = _CsrfManager()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P08] THREAD-SAFE SERVICE REGISTRY
# INSERT: replace any plain dict used to store service configs
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class _ServiceRegistry:
    """Thread-safe store for loaded service configurations."""
    def __init__(self):
        self._store: dict[str, dict] = {}
        self._lock  = threading.RLock()

    def register(self, alias: str, cfg: dict) -> None:
        with self._lock:
            self._store[alias] = cfg

    def get(self, alias: str) -> dict | None:
        with self._lock:
            return self._store.get(alias)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._store.values())

    def aliases(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._store)

_svc_registry = _ServiceRegistry()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P09] odata_service_info MCP TOOL
# INSERT: in your tool-generation loop, add one info tool per service alias
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _make_service_info_tool(alias: str, cfg: dict) -> dict:
    """
    Build a static MCP tool that returns metadata about the registered service.
    Call this tool from an agent to understand what entity sets, ops, and
    config are available â€” before issuing any data queries.
    """
    def _handler(_params: dict, _cfg: dict = cfg, _alias: str = alias) -> str:
        return json.dumps({
            "alias":              _alias,
            "url":                _cfg.get("url", ""),
            "entity_sets":        _cfg.get("_entity_names", []),
            "function_imports":   _cfg.get("_function_names", []),
            "enabled_ops":        _cfg.get("enable_ops", "SFGCUDA"),
            "readonly":           _cfg.get("readonly", False),
            "auth_type":          ("passthrough" if _cfg.get("passthrough")
                                   else "cookie" if _cfg.get("cookie_string")
                                   else "basic"   if _cfg.get("username")
                                   else "anonymous"),
            "convert_dates":      _cfg.get("convert_dates", True),
            "max_items":          _cfg.get("max_items", 100),
            "max_response_bytes": _cfg.get("max_response_size", 5 * 1024 * 1024),
            "bridge_version":     __version__,
        }, indent=2)

    return {
        "name":        f"{alias}__odata_service_info",
        "description": (
            f"Returns runtime metadata for OData service [{alias}]: "
            "URL, registered entity sets, function imports, enabled operations, "
            "auth type, and response limits. Call this before data tools."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "_handler":    _handler,
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P10] /health ENDPOINT
# INSERT: in your BaseHTTPRequestHandler.do_GET, add this route
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _health_response(registry: _ServiceRegistry) -> dict:
    """Return a structured health payload for the /health endpoint."""
    return {
        "status":    "ok",
        "version":   __version__,
        "pid":       os.getpid(),
        "services":  registry.aliases(),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }

# In your HTTPRequestHandler.do_GET:
#
#   if self.path == "/health":
#       body = json.dumps(_health_response(_svc_registry)).encode()
#       self.send_response(200)
#       self.send_header("Content-Type", "application/json")
#       self.send_header("Content-Length", str(len(body)))
#       self.end_headers()
#       self.wfile.write(body)
#       return


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P11] LOCALHOST-ONLY GUARD
# INSERT: in main(), before HTTPServer(host, port).serve_forever()
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _enforce_localhost(host: str, bypass: bool = False) -> None:
    """
    Prevent accidental exposure of the MCP HTTP endpoint on non-loopback
    interfaces. This mirrors Go's security default.

    Pass bypass=True (via --i-am-security-expert flag) to allow external binding.
    """
    loopback = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    # 0.0.0.0 is allowed only with bypass because it binds to all interfaces
    strict_loopback = {"127.0.0.1", "localhost", "::1"}
    if host not in strict_loopback and not bypass:
        _log.critical(
            "
"
            "  SECURITY BLOCK: HTTP transport would bind to '%s'.
"
            "  This exposes OData credentials and MCP tools to the network.
"
            "  Pass --i-am-security-expert to explicitly allow this.
"
            "  Recommended: use stdio transport for local agent connections.
",
            host
        )
        sys.exit(1)
    if host not in strict_loopback:
        _log.warning(
            "HTTP transport bound to %s â€” ensure this is in a secured network.", host
        )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P12] STARTUP BANNER
# INSERT: at the very start of main(), after args are parsed
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _startup_banner(args) -> None:
    _log.info("=" * 60)
    _log.info("JAM OData MCP Bridge  v%s  |  PID %d", __version__, os.getpid())
    _log.info("Transport : %s", getattr(args, "transport", "stdio"))
    if getattr(args, "transport", "stdio") == "http":
        _log.info("Listen    : http://%s:%d", getattr(args, "host", "127.0.0.1"),
                  getattr(args, "port", 7777))
        _log.info("Health    : http://%s:%d/health",
                  getattr(args, "host", "127.0.0.1"), getattr(args, "port", 7777))
    _log.info("Services  : %d loaded", _svc_registry.count())
    if getattr(args, "trace_mcp", False):
        _log.info("MCP trace : ENABLED")
    if getattr(args, "max_response_size", 0):
        _log.info("Max resp  : %s B", f"{args.max_response_size:,}")
    _log.info("=" * 60)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P13] NEW ARGPARSE ARGUMENTS
# INSERT: append these to your existing ArgumentParser before parse_args()
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_NEW_ARGS_DOC = """
# Add these to your existing parser = argparse.ArgumentParser(...) block:

parser.add_argument(
    "--version", action="version", version=f"%(prog)s {__version__}"
)
parser.add_argument(
    "--dotenv", metavar="PATH", default=".env",
    help="Path to .env file (default: .env). Loaded before config. "
         "Does not override already-set env vars."
)
parser.add_argument(
    "--max-response-size", type=int, default=5 * 1024 * 1024, metavar="BYTES",
    help="Hard byte cap on raw OData response (default: 5 MB). "
         "Returns a structured error with filter hints when exceeded."
)
parser.add_argument(
    "--trace-mcp", action="store_true",
    help="Write all MCP JSON-RPC messages to a temp file for debugging."
)
parser.add_argument(
    "--log-file", metavar="PATH",
    help="Write logs to this file (RotatingFileHandler, 10 MB / 3 backups)."
)
parser.add_argument(
    "--verbose", action="store_true",
    help="Enable DEBUG log level (default: INFO)."
)
parser.add_argument(
    "--localhost-only", action="store_true", default=True,
    help="Restrict HTTP transport to 127.0.0.1 (default: ON)."
)
parser.add_argument(
    "--i-am-security-expert", action="store_true", dest="security_bypass",
    help="Bypass --localhost-only to bind HTTP on non-loopback interface."
)
parser.add_argument(
    "--service-info-tool", action="store_true", default=True,
    help="Add odata_service_info tool per service alias (default: ON)."
)
parser.add_argument(
    "--no-service-info-tool", action="store_false", dest="service_info_tool",
    help="Disable the odata_service_info introspection tool."
)
"""


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [P14] GRACEFUL SHUTDOWN (improved)
# INSERT: replace or enhance your existing SIGTERM handler
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_shutdown_event = threading.Event()

def _handle_signal(signum, frame) -> None:
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    _log.info("Received %s â€” initiating graceful shutdown...", sig_name)
    _shutdown_event.set()
    if _mcp_trace_fh:
        try:
            _mcp_trace_fh.close()
        except Exception:
            pass
    sys.exit(0)

# Register in main():
#   signal.signal(signal.SIGTERM, _handle_signal)
#   signal.signal(signal.SIGINT,  _handle_signal)
#!/usr/bin/env python3
"""
JAM OData MCP Bridge (Enhanced v2.0)
======================================
Multi-service OData v4 -> MCP bridge server.

New in v2.0:
  - stdio transport (Claude Desktop / Claude Code / any MCP host)
  - SAP legacy /Date(ms)/ -> ISO-8601 conversion (default ON)
  - Granular op filtering: --enable / --disable (C/S/F/G/U/D/A/R)
  - MCP Bearer token auth: --mcp-token / --mcp-token-file
  - Wildcard entity filtering via fnmatch (Product*, Order*)
  - Cookie file (Netscape) and cookie-string authentication
  - Graceful SIGTERM / SIGINT shutdown
  - Claude Code friendly: strips $ from param names (-c flag)
  - --trace: dump all tools + exit (debug mode)
  - --read-only-but-functions: hide CUD, keep actions
  - --sort-tools / --no-sort-tools
  - --max-items: hard cap on returned rows + pagination hint
  - --verbose-errors: full HTTP detail in error responses

Original features preserved:
  - BTP Connectivity proxy (VCAP_SERVICES / CF on-premise tunnel)
  - Multi-service architecture with aliases (services.json)
  - Passthrough auth (forward caller credentials to OData)
  - CORS for Copilot Studio and browser clients
  - Count operation per entity set
  - Pure Python stdlib — zero external dependencies

Usage (stdio, Claude Desktop / Claude Code):
    python3 server.py --config services.json --transport stdio

Usage (HTTP, local dev):
    python3 server.py --config services.json --port 7777

Usage (HTTP + Bearer token):
    python3 server.py --config services.json --mcp-token mysecret

Usage (BTP CF):
    Set env: PORT, ODATA_USERNAME, ODATA_PASSWORD, MCP_USERNAME, MCP_PASSWORD
    python3 server.py --config services.json --passthrough

services.json example:
    [
      {
        "alias": "ean_e1",
        "url": "http://host/sap/opu/odata4/.../",
        "username": "${ODATA_USERNAME}",
        "password": "${ODATA_PASSWORD}",
        "passthrough": true,
        "include": ["Product*", "Order*"],
        "readonly": false,
        "enable_ops": "SFGCUDA"
      }
    ]
"""

import argparse
import base64
import datetime
import fnmatch
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import pathlib

# ---------------------------------------------------------------------------
# .env loader  (P01)
# Reads KEY=VALUE pairs before argparse so env vars are available to expand_env
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.
    Skips blank lines and # comments. Strips surrounding quotes from values.
    Does NOT override already-set env vars (safe for Docker / CF).
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
        val = val.strip().strip("\"'")   # strip straight single- and double-quotes
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# BTP Connectivity proxy  (on-premise access from CF)
# ---------------------------------------------------------------------------

_BTP_PROXY_URL:    str   = ""
_BTP_PROXY_TOKEN:  str   = ""
_BTP_TOKEN_EXPIRY: float = 0.0   # epoch seconds; 0 = not yet fetched
_BTP_TOKEN_URL:    str   = ""    # stored for refresh calls


def _btp_fetch_token(clientid: str, clientsecret: str, token_url: str) -> None:
    """Fetch a new BTP proxy token and update the global expiry timestamp."""
    global _BTP_PROXY_TOKEN, _BTP_TOKEN_EXPIRY
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    auth = base64.b64encode(f"{clientid}:{clientsecret}".encode()).decode()
    req  = urllib.request.Request(
        token_url, data=data,
        headers={
            "Authorization":  f"Basic {auth}",
            "Content-Type":   "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload           = json.loads(r.read())
        _BTP_PROXY_TOKEN  = payload["access_token"]
        ttl               = int(payload.get("expires_in", 3600))
        _BTP_TOKEN_EXPIRY = time.time() + ttl
        sys.stderr.write(
            f"[bridge] BTP token refreshed, expires in {ttl}s "
            f"(at {time.strftime('%H:%M:%S', time.localtime(_BTP_TOKEN_EXPIRY))})\n"
        )


def _get_btp_token() -> str:
    """Return a valid BTP proxy token, refreshing 60 s before expiry.

    Thread-safety: two concurrent refreshes at expiry boundary both produce
    a valid token, so no lock is needed.
    """
    global _BTP_PROXY_TOKEN, _BTP_TOKEN_EXPIRY
    if not _BTP_TOKEN_URL:
        return _BTP_PROXY_TOKEN          # BTP proxy not configured
    if time.time() < _BTP_TOKEN_EXPIRY - 60:
        return _BTP_PROXY_TOKEN          # still fresh

    # Token expired or about to expire - re-fetch
    vcap_raw = os.environ.get("VCAP_SERVICES", "")
    if not vcap_raw:
        return _BTP_PROXY_TOKEN
    try:
        vcap         = json.loads(vcap_raw)
        conn         = vcap.get("connectivity", [{}])[0].get("credentials", {})
        clientid     = conn.get("clientid", "")
        clientsecret = conn.get("clientsecret", "")
        _btp_fetch_token(clientid, clientsecret, _BTP_TOKEN_URL)
    except Exception as exc:
        sys.stderr.write(f"[bridge] BTP token refresh failed: {exc} - using stale token\n")
    return _BTP_PROXY_TOKEN


def _init_btp_proxy() -> None:
    """Read VCAP_SERVICES and obtain a proxy JWT for the Connectivity service."""
    global _BTP_PROXY_URL, _BTP_TOKEN_URL
    vcap_raw = os.environ.get("VCAP_SERVICES", "")
    if not vcap_raw:
        return
    try:
        vcap = json.loads(vcap_raw)
        conn = vcap.get("connectivity", [{}])[0].get("credentials", {})
        host         = conn.get("onpremise_proxy_host", "")
        port         = conn.get("onpremise_proxy_port", "20003")
        clientid     = conn.get("clientid", "")
        clientsecret = conn.get("clientsecret", "")
        token_url    = conn.get("token_service_url", "")
        if not all([host, clientid, clientsecret, token_url]):
            sys.stderr.write("[bridge] BTP connectivity: missing credentials in VCAP_SERVICES\n")
            return
        if not token_url.endswith("/oauth/token"):
            token_url = token_url.rstrip("/") + "/oauth/token"
        _BTP_TOKEN_URL = token_url
        _BTP_PROXY_URL = f"http://{host}:{port}"
        _btp_fetch_token(clientid, clientsecret, token_url)
        sys.stderr.write(f"[bridge] BTP connectivity proxy: {_BTP_PROXY_URL}\n")
    except Exception as exc:
        sys.stderr.write(f"[bridge] BTP connectivity init failed: {exc}\n")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDM_NS       = "http://docs.oasis-open.org/odata/ns/edm"
HTTP_TIMEOUT = 30

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def edm_to_json(edm_type: str) -> str:
    return EDM_TO_JSON.get(edm_type, "string")


def expand_env(value: str) -> str:
    """Expand ${VAR} placeholders in config strings."""
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------------------
# NEW: SAP legacy date conversion  /Date(msÂ±offset)/ -> ISO-8601
# ---------------------------------------------------------------------------

def convert_legacy_dates(obj):
    """
    Recursively walk a JSON-decoded structure and replace every
    /Date(timestamp)/ string with a proper ISO-8601 UTC string.
    Example: /Date(1748736000000)/ -> "2025-04-01T00:00:00Z"
    """
    if isinstance(obj, str):
        m = _LEGACY_DATE_RE.fullmatch(obj)
        if m:
            ms = int(m.group(1))
            dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return obj
    if isinstance(obj, dict):
        return {k: convert_legacy_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_legacy_dates(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# NEW: Granular operation filter  (C / S / F / G / U / D / A / R)
# ---------------------------------------------------------------------------

def _expand_op_string(ops: str) -> set:
    """Expand the shorthand R -> {S,F,G} and return a set of single-char codes.
    Warns on unrecognised characters so misconfigurations are visible."""
    out: set = set()
    valid = set("CSFGUDAR")
    for ch in ops.upper():
        if ch == OP_READ:
            out.update({OP_SEARCH, OP_FILTER, OP_GET})
        elif ch in "CSFGUDA":
            out.add(ch)
        elif ch not in valid:
            sys.stderr.write(
                f"[bridge] warning: unknown op code '{ch}' in op filter "
                f"(valid: C S F G U D A R)\n"
            )
    return out


class OpFilter:
    """
    Decides which operation types are visible for a service.

    Priority (highest to lowest):
        readonly=True                -> allow only {S,F,G}
        readonly_but_functions=True  -> allow {S,F,G,A}
        enable_ops string            -> allow exactly those ops
        disable_ops string           -> allow all except those ops
        (none)                       -> allow everything
    """

    def __init__(self, enable_ops: str = "", disable_ops: str = "",
                 readonly: bool = False, readonly_but_functions: bool = False):
        if enable_ops and disable_ops:
            raise ValueError("enable_ops and disable_ops are mutually exclusive")

        if readonly:
            self._allowed: set | None = {OP_SEARCH, OP_FILTER, OP_GET}
        elif readonly_but_functions:
            self._allowed = {OP_SEARCH, OP_FILTER, OP_GET, OP_ACTION}
        elif enable_ops:
            self._allowed = _expand_op_string(enable_ops)
        elif disable_ops:
            self._allowed = set("CSFGUDA") - _expand_op_string(disable_ops)
        else:
            self._allowed = None  # no restriction — allow all

    def allows(self, op: str) -> bool:
        if self._allowed is None:
            return True
        return op.upper() in self._allowed


# ---------------------------------------------------------------------------
# NEW: Wildcard entity matching via fnmatch
# ---------------------------------------------------------------------------

def matches_patterns(name: str, patterns: list) -> bool:
    """True if *name* matches any pattern (supports * and ? wildcards)."""
    if not patterns:
        return True
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# ---------------------------------------------------------------------------
# Input guard  (P04)
# ---------------------------------------------------------------------------

_MAX_STRING_PARAM = 4096  # chars; prevents oversized filter/search strings

def _guard_params(params: dict) -> dict:
    """Sanitise MCP tool arguments: strip null bytes, truncate over-long strings."""
    out = {}
    for k, v in params.items():
        if isinstance(v, str):
            v = v.replace("\x00", "")
            if len(v) > _MAX_STRING_PARAM:
                sys.stderr.write(
                    f"[bridge] warning: param '{k}' truncated "
                    f"({len(v)} -> {_MAX_STRING_PARAM} chars)\n"
                )
                v = v[:_MAX_STRING_PARAM]
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# NEW: Cookie helpers
# ---------------------------------------------------------------------------

def load_cookies_from_file(path: str) -> dict:
    """
    Parse a Netscape / curl cookie file into a {name: value} dict.
    Lines starting with # are comments; expects tab-separated fields.
    """
    cookies: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
                elif "=" in line:
                    k, _, v = line.partition("=")
                    cookies[k.strip()] = v.strip()
    except OSError as exc:
        sys.stderr.write(f"[bridge] cookie file load error: {exc}\n")
    return cookies


def parse_cookie_string(cookie_str: str) -> dict:
    """Parse 'key1=val1; key2=val2' -> {key1: val1, key2: val2}.
    Uses http.cookies.SimpleCookie to handle quoted values and escapes correctly.
    Falls back to manual split if the string can't be parsed."""
    import http.cookies
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(cookie_str)
        return {k: m.value for k, m in jar.items()}
    except http.cookies.CookieError:
        # Manual fallback for malformed but common cookie strings
        cookies: dict = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        return cookies


# ---------------------------------------------------------------------------
# Threading HTTP Server
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# ODataService — one configured OData endpoint
# ---------------------------------------------------------------------------

class ODataService:
    def __init__(
        self,
        alias:                  str,
        url:                    str,
        username:               str  = "",
        password:               str  = "",
        passthrough:            bool = False,
        include:                list = None,
        readonly:               bool = False,
        readonly_but_functions: bool = False,
        include_actions:        list = None,
        enable_ops:             str  = "",
        disable_ops:            str  = "",
        default_top:            int  = 50,
        max_top:                int  = 500,
        legacy_dates:           bool = True,
        claude_code_friendly:   bool = False,
        cookie_file:            str  = "",
        cookie_string:          str  = "",
        verbose_errors:         bool = False,
        max_items:              int  = 100,
        max_response_size:      int  = 5 * 1024 * 1024,
    ):
        self.alias                = alias
        self.url                  = url.rstrip("/")
        self.username             = username
        self.password             = password
        self.passthrough          = passthrough   # forward caller credentials to OData
        self.legacy_dates         = legacy_dates
        self.claude_code_friendly = claude_code_friendly
        self.verbose_errors       = verbose_errors
        self.max_items            = max_items
        self.max_response_size    = max_response_size

        # Granular operation filter (replaces the old bool readonly flag)
        self.op_filter = OpFilter(
            enable_ops             = enable_ops,
            disable_ops            = disable_ops,
            readonly               = readonly,
            readonly_but_functions = readonly_but_functions,
        )
        self.readonly        = readonly          # kept for backwards compat
        self.include_actions = set(include_actions) if include_actions else None
        self.default_top     = default_top       # default $top cap for filter queries
        self.max_top         = max_top           # hard upper cap on $top

        self.entity_sets: dict[str, dict] = {}
        self.actions:     list[dict]      = []
        self.schema_ns                    = ""

        # Cookie auth (loaded before _make_opener is called)
        self._extra_cookies: dict = {}
        if cookie_file:
            self._extra_cookies = load_cookies_from_file(cookie_file)
            sys.stderr.write(
                f"[bridge] {alias}: loaded {len(self._extra_cookies)} cookies from file\n"
            )
        elif cookie_string:
            self._extra_cookies = parse_cookie_string(cookie_string)
            sys.stderr.write(
                f"[bridge] {alias}: parsed {len(self._extra_cookies)} cookies from string\n"
            )

        self._csrf_token        = ""
        self._bootstrap_opener  = self._make_opener(username, password)
        self._load_metadata()

        # Apply entity-set whitelist (now supports fnmatch wildcards)
        if include:
            self.entity_sets = {
                k: v for k, v in self.entity_sets.items()
                if matches_patterns(k, include)
            }

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _make_opener(
        self,
        username:    str = "",
        password:    str = "",
        auth_header: str = "",
    ) -> urllib.request.OpenerDirector:

        handlers: list = [urllib.request.HTTPCookieProcessor(CookieJar())]

        # BTP on-premise proxy
        if _BTP_PROXY_URL:
            class _ProxyAuth(urllib.request.BaseHandler):
                handler_order = 490

                def http_request(self, req):
                    req.add_unredirected_header(
                        "Proxy-Authorization", f"Bearer {_get_btp_token()}"
                    )
                    return req

                https_request = http_request

            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": _BTP_PROXY_URL, "https": _BTP_PROXY_URL}
                )
            )
            handlers.append(_ProxyAuth())

        opener = urllib.request.build_opener(*handlers)

        # Determine Authorization header value
        if auth_header:
            av = auth_header
        elif username:
            av = "Basic " + base64.b64encode(
                f"{username}:{password}".encode()
            ).decode()
        else:
            av = ""

        extra_cookies = self._extra_cookies

        if av or extra_cookies:
            _av      = av
            _cookies = extra_cookies

            class _Auth(urllib.request.BaseHandler):
                def http_request(self, req):
                    if _av:
                        req.add_unredirected_header("Authorization", _av)
                    if _cookies:
                        cookie_hdr = "; ".join(
                            f"{k}={v}" for k, v in _cookies.items()
                        )
                        req.add_unredirected_header("Cookie", cookie_hdr)
                    return req

                https_request = http_request

            opener.add_handler(_Auth())

        return opener

    def _opener(
        self, auth_header: str = ""
    ) -> urllib.request.OpenerDirector:
        if self.passthrough and auth_header:
            return self._make_opener(auth_header=auth_header)
        return self._bootstrap_opener

    def _open(self, req, auth_header: str = ""):
        return self._opener(auth_header).open(req, timeout=HTTP_TIMEOUT)

    def _fetch_csrf(self, opener: urllib.request.OpenerDirector) -> str:
        """
        Fetch a CSRF token using the same session as the upcoming write request.
        Uses $metadata as the fetch endpoint (light-weight, always available).
        """
        url = f"{self.url}/$metadata"
        req = urllib.request.Request(url, headers={"x-csrf-token": "Fetch"})
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as r:
                return r.headers.get("x-csrf-token", "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # $metadata loader                                                     #
    # ------------------------------------------------------------------ #

    def _load_metadata(self) -> None:
        url = f"{self.url}/$metadata"
        req = urllib.request.Request(url)
        try:
            with self._open(req) as r:
                raw = r.read()
        except Exception as exc:
            sys.stderr.write(f"[bridge] {self.alias}: metadata load failed: {exc}\n")
            return

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            sys.stderr.write(f"[bridge] {self.alias}: metadata parse failed: {exc}\n")
            return

        # Try OData v4 namespace first, then fall back to v2/v3 (SAP NetWeaver etc.)
        _EDM_FALLBACKS = [
            "http://docs.oasis-open.org/odata/ns/edm",
            "http://schemas.microsoft.com/ado/2008/09/edm",
            "http://schemas.microsoft.com/ado/2007/05/edm",
            "http://schemas.microsoft.com/ado/2006/04/edm",
        ]
        schema = None
        detected_ns = EDM_NS
        for _ns_uri in _EDM_FALLBACKS:
            schema = root.find(f".//{{{_ns_uri}}}Schema")
            if schema is not None:
                detected_ns = _ns_uri
                break
        ns = {"edm": detected_ns}
        if schema is None:
            sys.stderr.write(
                f"[bridge] {self.alias}: no Schema element found in $metadata\n"
            )
            return

        self.schema_ns = schema.get("Namespace", "")

        # ---- EntityType map ----
        entity_types: dict[str, dict] = {}
        for et in schema.findall("edm:EntityType", ns):
            et_name = et.get("Name", "")
            keys = [
                kp.get("Name", "")
                for kp in et.findall(".//edm:PropertyRef", ns)
            ]
            props: dict = {}
            for prop in et.findall("edm:Property", ns):
                pname    = prop.get("Name", "")
                ptype    = prop.get("Type", "Edm.String")
                nullable = prop.get("Nullable", "true").lower() != "false"
                props[pname] = {
                    "type":     edm_to_json(ptype),
                    "edm_type": ptype,
                    "nullable": nullable,
                }
            nav_props = [
                np.get("Name", "")
                for np in et.findall("edm:NavigationProperty", ns)
            ]
            entity_types[et_name] = {
                "keys":      keys,
                "props":     props,
                "nav_props": nav_props,
            }

        # ---- EntitySet -> EntityType mapping ----
        for ec in schema.findall(".//edm:EntitySet", ns):
            es_name = ec.get("Name", "")
            et_name = ec.get("EntityType", "").split(".")[-1]
            if et_name in entity_types:
                self.entity_sets[es_name] = entity_types[et_name]

        # ---- Actions and Functions (OData v4) ----
        for node in (
            list(schema.findall("edm:Action", ns))
            + list(schema.findall("edm:Function", ns))
        ):
            a_name = node.get("Name", "")
            is_bound     = node.get("IsBound", "false").lower() == "true"
            is_collection = False
            entity_set   = ""
            params: list = []

            for param in node.findall("edm:Parameter", ns):
                pname = param.get("Name", "")
                ptype = param.get("Type", "Edm.String")
                # Detect bound parameter (first param of bound action)
                if is_bound and not params:
                    # Collection(Namespace.Type) indicates collection-bound
                    if ptype.startswith("Collection("):
                        is_collection = True
                        inner_type = ptype[11:-1].split(".")[-1]
                        entity_set = next(
                            (
                                k for k, v in entity_types.items()
                                if k == inner_type
                            ),
                            "",
                        )
                    continue   # skip the binding parameter from tool params
                params.append({"name": pname, "type": edm_to_json(ptype)})

            self.actions.append({
                "name":                a_name,
                "is_bound":            is_bound,
                "is_collection_bound": is_collection,
                "entity_set":          entity_set,
                "params":              params,
            })

        # Apply action whitelist
        if self.include_actions is not None:
            self.actions = [
                a for a in self.actions
                if a["name"] in self.include_actions
            ]

        sys.stderr.write(
            f"[bridge] {self.alias}: "
            f"{len(self.entity_sets)} entity sets, "
            f"{len(self.actions)} actions\n"
        )

    # ------------------------------------------------------------------ #
    # OData operations                                                     #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method:        str,
        url:           str,
        body:          dict = None,
        extra_headers: dict = None,
        auth_header:   str  = "",
    ) -> dict:
        headers = {"Accept": "application/json", "OData-Version": "4.0"}
        if extra_headers:
            headers.update(extra_headers)

        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with self._open(req, auth_header) as r:
                raw = r.read()
                if self.max_response_size > 0 and len(raw) > self.max_response_size:
                    sys.stderr.write(
                        f"[bridge] {self.alias}: response capped "
                        f"({len(raw):,} B > {self.max_response_size:,} B limit)\n"
                    )
                    return {
                        "error": "RESPONSE_TOO_LARGE",
                        "message": (
                            f"OData response ({len(raw):,} B) exceeds "
                            f"--max-response-size ({self.max_response_size:,} B). "
                            "Narrow your query with $top, $select, or $filter."
                        ),
                    }
                result = json.loads(raw) if raw else {}
                if self.legacy_dates:
                    result = convert_legacy_dates(result)
                return result
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode(errors="replace")
            except Exception:
                pass
            if self.verbose_errors:
                return {
                    "error":       str(exc),
                    "http_status": exc.code,
                    "url":         url,
                    "method":      method,
                    "detail":      body_text,
                }
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:
            if self.verbose_errors:
                return {"error": str(exc), "url": url, "method": method}
            return {"error": str(exc)}

    def filter(self, entity_set: str, args: dict, auth: str = "") -> dict:
        # Apply default_top / max_top guard
        if args.get("top") is None and self.default_top:
            args["top"] = self.default_top
        if self.max_top and args.get("top"):
            args["top"] = min(int(args["top"]), self.max_top)

        params: dict = {}
        if args.get("filter"):
            params["$filter"]  = args["filter"]
        if args.get("top"):
            params["$top"]     = str(args["top"])
        if args.get("skip"):
            params["$skip"]    = str(args["skip"])
        if args.get("select"):
            params["$select"]  = args["select"]
        if args.get("orderby"):
            params["$orderby"] = args["orderby"]
        if args.get("expand"):
            params["$expand"]  = args["expand"]

        qs  = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in params.items()
        )
        url = f"{self.url}/{entity_set}" + (f"?{qs}" if qs else "")
        result = self._request("GET", url, auth_header=auth)

        # max_items cap + pagination hint
        if isinstance(result, dict):
            items = result.get("value", [])
            top   = int(args.get("top") or 0)
            if items and len(items) > self.max_items:
                result["value"] = items[: self.max_items]
                result["pagination_hint"] = (
                    f"Truncated to {self.max_items} records (--max-items limit). "
                    f"Use $skip={self.max_items} for the next page, "
                    f"or add $filter to narrow results."
                )
            elif items and top and len(items) >= top:
                result["pagination_hint"] = (
                    f"Returned {len(items)} records (limit={top}). "
                    f"Use $skip={len(items)} for the next page, "
                    f"or call {self.alias}_count_{entity_set} to get the total."
                )
        return result

    def count(self, entity_set: str, filter_expr: str = "", auth: str = "") -> dict:
        url = f"{self.url}/{entity_set}/$count"
        if filter_expr:
            url += f"?$filter={urllib.parse.quote(filter_expr, safe='')}"
        return self._request("GET", url, auth_header=auth)

    def get(self, entity_set: str, key: str, args: dict, auth: str = "") -> dict:
        qs_parts: dict = {}
        if args.get("$select"):
            qs_parts["$select"] = args["$select"]
        if args.get("$expand"):
            qs_parts["$expand"] = args["$expand"]
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in qs_parts.items()
        )
        url = f"{self.url}/{entity_set}({key})" + (f"?{qs}" if qs else "")
        return self._request("GET", url, auth_header=auth)

    def create(self, entity_set: str, body: dict, auth: str = "") -> dict:
        opener = self._opener(auth)
        csrf   = self._fetch_csrf(opener)
        return self._request(
            "POST",
            f"{self.url}/{entity_set}",
            body=body,
            extra_headers={"x-csrf-token": csrf} if csrf else None,
            auth_header=auth,
        )

    def update(
        self, entity_set: str, key: str, body: dict, auth: str = ""
    ) -> dict:
        opener = self._opener(auth)
        csrf   = self._fetch_csrf(opener)
        return self._request(
            "PATCH",
            f"{self.url}/{entity_set}({key})",
            body=body,
            extra_headers={"x-csrf-token": csrf} if csrf else None,
            auth_header=auth,
        )

    def delete(self, entity_set: str, key: str, auth: str = "") -> dict:
        opener = self._opener(auth)
        csrf   = self._fetch_csrf(opener)
        return self._request(
            "DELETE",
            f"{self.url}/{entity_set}({key})",
            extra_headers={"x-csrf-token": csrf} if csrf else None,
            auth_header=auth,
        )

    def call_action(
        self, action_name: str, params: dict, auth: str = ""
    ) -> dict:
        opener = self._opener(auth)
        csrf   = self._fetch_csrf(opener)
        fqn    = (
            f"{self.schema_ns}.{action_name}" if self.schema_ns else action_name
        )
        return self._request(
            "POST",
            f"{self.url}/{fqn}",
            body=params,
            extra_headers={"x-csrf-token": csrf} if csrf else None,
            auth_header=auth,
        )

    # ------------------------------------------------------------------ #
    # Tool-name param helper (Claude Code friendly mode)                  #
    # ------------------------------------------------------------------ #

    def _strip_dollar(self, name: str) -> str:
        """In Claude Code friendly mode, expose 'filter' instead of '$filter'."""
        if self.claude_code_friendly and name.startswith("$"):
            return name[1:]
        return name


# ---------------------------------------------------------------------------
# Bridge — manages multiple services, generates & dispatches MCP tools
# ---------------------------------------------------------------------------

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

    def _index_tool(self, svc: ODataService, name: str) -> None:
        rest = name[len(svc.alias) + 1:]
        for op in ("filter", "count", "get", "create", "update", "delete", "action"):
            if rest.startswith(op + "_"):
                self._tool_map[name] = (svc, op, rest[len(op) + 1:])
                return

    def _gen_tools(self, svc: ODataService) -> list:
        tools:    list = []
        a = svc.alias

        # ---- Service info tool (P09) ----
        tools.append({
            "name": f"{a}__info",
            "description": (
                f"Returns metadata for OData service [{a}]: URL, entity sets, "
                "actions, enabled operations, auth type, and response limits. "
                "Call this first to understand what tools are available."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        })
        self._tool_map[f"{a}__info"] = (svc, "info", a)

        # ---- Entity set tools ----
        for es_name, es in svc.entity_sets.items():
            keys      = es["keys"]
            props     = es["props"]
            nav_props = es.get("nav_props", [])

            field_summary = ", ".join(
                f"{k}({v['edm_type']})"
                for k, v in list(props.items())[:10]
            )
            nav_summary = (
                f" Nav: {', '.join(nav_props[:5])}" if nav_props else ""
            )
            full_desc = f"Total fields: {len(props)}.{nav_summary}"

            # --- filter ---
            if svc.op_filter.allows(OP_FILTER):
                filter_params = {
                    svc._strip_dollar("$filter"):  {
                        "type": "string",
                        "description": "OData $filter expression, e.g. Name eq 'test'",
                    },
                    svc._strip_dollar("$top"):     {
                        "type": "integer",
                        "description": f"Max records to return (server cap: {svc.max_top})",
                    },
                    svc._strip_dollar("$skip"):    {
                        "type": "integer",
                        "description": "Number of records to skip (pagination offset)",
                    },
                    svc._strip_dollar("$select"):  {
                        "type": "string",
                        "description": "Comma-separated list of fields to return",
                    },
                    svc._strip_dollar("$orderby"): {
                        "type": "string",
                        "description": "Sort expression, e.g. Name asc",
                    },
                    svc._strip_dollar("$expand"):  {
                        "type": "string",
                        "description": (
                            f"Navigation properties to expand: "
                            f"{', '.join(nav_props[:5]) or 'none'}"
                        ),
                    },
                }
                tools.append({
                    "name": f"{a}_filter_{es_name}",
                    "description": (
                        f"Filter / search {es_name} records from the {a} service. "
                        f"Key fields: {field_summary}. {full_desc}"
                    ),
                    "inputSchema": {
                        "type":       "object",
                        "properties": filter_params,
                    },
                })

            # --- count ---
            if svc.op_filter.allows(OP_FILTER) or svc.op_filter.allows(OP_GET):
                tools.append({
                    "name": f"{a}_count_{es_name}",
                    "description": (
                        f"Return the total count of {es_name} records in {a}. "
                        f"Optionally pass $filter to count a subset."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            svc._strip_dollar("$filter"): {
                                "type": "string",
                                "description": "OData $filter expression",
                            }
                        },
                    },
                })

            # --- get ---
            if svc.op_filter.allows(OP_GET):
                key_schema = {
                    k: {
                        "type":        props[k]["type"] if k in props else "string",
                        "description": f"Key field: {k}",
                    }
                    for k in keys
                }
                tools.append({
                    "name": f"{a}_get_{es_name}",
                    "description": (
                        f"Retrieve a single {es_name} record by its key from {a}."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            **key_schema,
                            svc._strip_dollar("$select"): {
                                "type":        "string",
                                "description": "Comma-separated fields to return",
                            },
                            svc._strip_dollar("$expand"): {
                                "type":        "string",
                                "description": (
                                    f"Navigation properties: "
                                    f"{', '.join(nav_props[:5]) or 'none'}"
                                ),
                            },
                        },
                        "required": list(key_schema.keys()),
                    },
                })

            # --- create ---
            if svc.op_filter.allows(OP_CREATE):
                required_fields = [
                    k for k, v in props.items()
                    if not v["nullable"] and k not in keys
                ]
                tools.append({
                    "name": f"{a}_create_{es_name}",
                    "description": f"Create a new {es_name} record in {a}.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            k: {
                                "type":        v["type"],
                                "description": f"{k} ({v['edm_type']})",
                            }
                            for k, v in props.items()
                        },
                        "required": required_fields,
                    },
                })

            # --- update ---
            if svc.op_filter.allows(OP_UPDATE):
                key_schema = {
                    k: {
                        "type":        props[k]["type"] if k in props else "string",
                        "description": f"Key field: {k}",
                    }
                    for k in keys
                }
                patch_props = {
                    k: {
                        "type":        v["type"],
                        "description": f"{k} ({v['edm_type']})",
                    }
                    for k, v in props.items()
                    if k not in keys
                }
                tools.append({
                    "name": f"{a}_update_{es_name}",
                    "description": (
                        f"Update (PATCH) an existing {es_name} record in {a}."
                    ),
                    "inputSchema": {
                        "type":       "object",
                        "properties": {**key_schema, **patch_props},
                        "required":   list(key_schema.keys()),
                    },
                })

            # --- delete ---
            if svc.op_filter.allows(OP_DELETE):
                key_schema = {
                    k: {
                        "type":        props[k]["type"] if k in props else "string",
                        "description": f"Key field: {k}",
                    }
                    for k in keys
                }
                tools.append({
                    "name": f"{a}_delete_{es_name}",
                    "description": f"Delete a {es_name} record from {a}.",
                    "inputSchema": {
                        "type":       "object",
                        "properties": key_schema,
                        "required":   list(key_schema.keys()),
                    },
                })

        # ---- Action / Function tools ----
        if svc.op_filter.allows(OP_ACTION):
            for action in svc.actions:
                a_name  = action["name"]
                p_props = {
                    p["name"]: {"type": p["type"]}
                    for p in action["params"]
                }
                desc = f"Call OData action/function {a_name} on {a}."
                if action.get("is_bound") and action.get("entity_set"):
                    desc += (
                        f" Bound to {action['entity_set']} "
                        f"({'collection' if action['is_collection_bound'] else 'single entity'})."
                    )
                tools.append({
                    "name": f"{a}_action_{a_name}",
                    "description": desc,
                    "inputSchema": {
                        "type":       "object",
                        "properties": p_props,
                    },
                })

        return tools

    # ------------------------------------------------------------------ #
    # MCP dispatch                                                         #
    # ------------------------------------------------------------------ #

    def handle(self, req: dict, auth_header: str = "") -> dict | None:
        method  = req.get("method", "")
        req_id  = req.get("id")
        params  = req.get("params", {})

        def ok(result):
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        def err(code, msg):
            return {
                "jsonrpc": "2.0",
                "id":      req_id,
                "error":   {"code": code, "message": msg},
            }

        # ---- Protocol handshake ----
        if method == "initialize":
            return ok({
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "serverInfo":      {"name": "odata-mcp-bridge", "version": "2.0.0"},
                "capabilities":    {"tools": {}},
            })

        if method in ("notifications/initialized", "initialized"):
            return None   # one-way notification — no response

        # ---- Tool listing ----
        if method == "tools/list":
            return ok({"tools": self._all_tools})

        # ---- Tool call ----
        if method == "tools/call":
            tool_name = params.get("name", "")
            args      = _guard_params(dict(params.get("arguments", {})))

            entry = self._tool_map.get(tool_name)
            if not entry:
                return err(-32601, f"Unknown tool: {tool_name}")

            svc, op, target = entry

            # Claude Code friendly: re-attach $ prefix for internal OData use
            if svc.claude_code_friendly:
                odata_params = {
                    "filter", "top", "skip", "select", "orderby", "expand"
                }
                args = {
                    (f"${k}" if k in odata_params else k): v
                    for k, v in args.items()
                }

            try:
                if op == "info":
                    result = {
                        "alias":              svc.alias,
                        "url":                svc.url,
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
                    }

                elif op == "filter":
                    result = svc.filter(target, args, auth=auth_header)

                elif op == "count":
                    filter_key = "$filter" if not svc.claude_code_friendly else "filter"
                    result = svc.count(
                        target,
                        args.get("$filter", args.get("filter", "")),
                        auth=auth_header,
                    )

                elif op == "get":
                    keys      = svc.entity_sets[target]["keys"]
                    key_parts = []
                    for k in keys:
                        v = args.pop(k, "")
                        key_parts.append(
                            f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}"
                        )
                    result = svc.get(
                        target, ",".join(key_parts), args, auth=auth_header
                    )

                elif op == "create":
                    result = svc.create(target, args, auth=auth_header)

                elif op == "update":
                    keys      = svc.entity_sets[target]["keys"]
                    key_parts = []
                    for k in keys:
                        v = args.pop(k, "")
                        key_parts.append(
                            f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}"
                        )
                    result = svc.update(
                        target, ",".join(key_parts), args, auth=auth_header
                    )

                elif op == "delete":
                    keys      = svc.entity_sets[target]["keys"]
                    key_parts = []
                    for k in keys:
                        v = args.pop(k, "")
                        key_parts.append(
                            f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}"
                        )
                    result = svc.delete(
                        target, ",".join(key_parts), auth=auth_header
                    )

                elif op == "action":
                    result = svc.call_action(target, args, auth=auth_header)

                else:
                    return err(-32601, f"Unknown op: {op}")

            except Exception as exc:
                return err(-32603, str(exc))

            return ok({
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ]
            })

        if method == "ping":
            return ok({})

        return err(-32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# NEW: Trace mode — print all registered tools and exit
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
# NEW: stdio transport  (Claude Desktop / Claude Code / any MCP host)
# ---------------------------------------------------------------------------

def run_stdio(bridge: Bridge, verbose: bool = False) -> None:
    """
    Read JSON-RPC requests from stdin (one per line) and write responses
    to stdout.  This is the standard MCP stdio transport.
    """
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
    bridge:      Bridge,
    mcp_token:   str = "",
    passthrough: bool = False,
):
    _mcp_token   = mcp_token
    _passthrough = passthrough

    class MCPHandler(BaseHTTPRequestHandler):
        protocol_version  = "HTTP/1.1"   # keep-alive: prevents 502 on CF GoRouter connection reuse
        mcp_username: str = ""
        mcp_password: str = ""

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[bridge] {self.address_string()} {fmt % args}\n")

        # ---- CORS headers ----
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin",  "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type,Authorization,Accept",
            )

        # ---- MCP-level auth check ----
        def _auth_ok(self) -> bool:
            ah = self.headers.get("Authorization", "")

            # Bearer token (preferred)
            if _mcp_token:
                if ah == f"Bearer {_mcp_token.strip()}":
                    return True
                # Also accept Basic with mcp_username/mcp_password when token set
                if ah.startswith("Basic ") and MCPHandler.mcp_username:
                    try:
                        decoded = base64.b64decode(ah[6:]).decode()
                        u, _, p = decoded.partition(":")
                        if u == MCPHandler.mcp_username and p == MCPHandler.mcp_password:
                            return True
                    except Exception:
                        pass
                return False

            # Basic auth only
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

            return True   # no auth configured — open

        # ---- Passthrough: return caller's auth header ----
        def _caller_auth(self) -> str:
            if _passthrough:
                return self.headers.get("Authorization", "")
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
            elif self.path == "/mcp":
                # 405 tells mcp-remote / SSE clients to fall back to POST-only mode
                self.send_response(405)
                self.send_header("Allow",          "POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_POST(self):
            if self.path not in ("/mcp", "/"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if not self._auth_ok():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.send_header("WWW-Authenticate", 'Bearer realm="mcp"')
                self._cors()
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)

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

            resp = bridge.handle(req, auth_header=self._caller_auth())
            if resp is not None:
                self._send_json(resp)
            else:
                # JSON-RPC notification (no id) — must still send an HTTP response
                # or the client hangs waiting. 202 Accepted per MCP Streamable HTTP spec.
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


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_services(config_path: str, cli_args) -> list:
    with open(config_path) as fh:
        cfg = json.load(fh)

    services: list = []
    for svc_cfg in cfg:
        alias      = svc_cfg.get("alias", "svc")
        url        = expand_env(svc_cfg.get("url", ""))
        username   = expand_env(svc_cfg.get("username", ""))
        password   = expand_env(svc_cfg.get("password", ""))
        passthrough        = svc_cfg.get("passthrough",            False)
        include            = svc_cfg.get("include",                None)
        readonly           = svc_cfg.get("readonly",               False)
        robf               = svc_cfg.get("readonly_but_functions", False)
        include_actions    = svc_cfg.get("include_actions",        None)
        enable_ops         = svc_cfg.get("enable_ops",  getattr(cli_args, "enable",  ""))
        disable_ops        = svc_cfg.get("disable_ops", getattr(cli_args, "disable", ""))
        default_top        = svc_cfg.get("default_top", 50)
        max_top            = svc_cfg.get("max_top",     500)
        cookie_file        = svc_cfg.get("cookie_file",   getattr(cli_args, "cookie_file",   ""))
        cookie_string      = svc_cfg.get("cookie_string", getattr(cli_args, "cookie_string", ""))

        # CLI flags override per-service config
        if getattr(cli_args, "read_only",               False):
            readonly = True
        if getattr(cli_args, "read_only_but_functions", False):
            robf     = True

        services.append(ODataService(
            alias                  = alias,
            url                    = url,
            username               = username,
            password               = password,
            passthrough            = passthrough,
            include                = include,
            readonly               = readonly,
            readonly_but_functions = robf,
            include_actions        = include_actions,
            enable_ops             = enable_ops,
            disable_ops            = disable_ops,
            default_top            = default_top,
            max_top                = max_top,
            legacy_dates           = not getattr(cli_args, "no_legacy_dates",      False),
            claude_code_friendly   = getattr(cli_args, "claude_code_friendly",    False),
            cookie_file            = cookie_file,
            cookie_string          = cookie_string,
            verbose_errors         = getattr(cli_args, "verbose_errors",          False),
            max_items              = getattr(cli_args, "max_items",               100),
            max_response_size      = getattr(cli_args, "max_response_size", 5 * 1024 * 1024),
        ))
    return services


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Load .env before argparse so ${VAR} placeholders in services.json resolve
    _load_dotenv()

    parser = argparse.ArgumentParser(
        prog        = "server.py",
        description = "JAM OData MCP Bridge v2 - OData v4 to MCP (Enhanced)",
    )

    # ---- Core ----
    parser.add_argument(
        "--config", default=os.environ.get("CONFIG_FILE", "services.json"),
        help="Path to services.json config file (env: CONFIG_FILE)",
    )
    parser.add_argument(
        "--dotenv", default=".env", metavar="PATH",
        help="Path to .env file loaded before config (default: .env)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("BRIDGE_HOST", "127.0.0.1"),
        help="HTTP bind address (default: 127.0.0.1, env: BRIDGE_HOST). "
             "Non-localhost addresses require --i-am-security-expert.",
    )
    parser.add_argument(
        "--i-am-security-expert", dest="security_bypass", action="store_true",
        default=False,
        help="Allow binding to a non-localhost address (required when --host is not 127.0.0.1/::1/localhost).",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("PORT", 7777)),
        help="HTTP port (http transport only)",
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="http",
        help="Transport type: stdio (Claude Desktop/Code) or http (default)",
    )

    # ---- MCP auth ----
    parser.add_argument(
        "--username", default=os.environ.get("MCP_USERNAME", ""),
        help="MCP Basic auth username",
    )
    parser.add_argument(
        "--password", default=os.environ.get("MCP_PASSWORD", ""),
        help="MCP Basic auth password",
    )
    parser.add_argument(
        "--passthrough", action="store_true",
        help="Forward caller Authorization header to OData services",
    )
    parser.add_argument(
        "--mcp-token", default=os.environ.get("MCP_TOKEN", ""),
        help="Bearer token for MCP auth (preferred over Basic)",
    )
    parser.add_argument(
        "--mcp-token-file", default="",
        help="Path to file containing MCP bearer token",
    )

    # ---- Cookie auth ----
    parser.add_argument(
        "--cookie-file", default="",
        help="Netscape-format cookie file for OData auth",
    )
    parser.add_argument(
        "--cookie-string", default="",
        help="Cookie string for OData auth: 'key1=val1; key2=val2'",
    )

    # ---- Operation filtering ----
    parser.add_argument(
        "--enable", default="",
        help=(
            "Enable ONLY these op types "
            "(C=create S=search F=filter G=get U=update D=delete A=action R=read[SFG])"
        ),
    )
    parser.add_argument(
        "--disable", default="",
        help="Disable these op types (same letter codes as --enable)",
    )
    parser.add_argument(
        "--read-only", "--ro", dest="read_only", action="store_true",
        help="Read-only mode: hide all create/update/delete + actions",
    )
    parser.add_argument(
        "--read-only-but-functions", "--robf",
        dest="read_only_but_functions", action="store_true",
        help="Read-only but keep action/function tools visible",
    )

    # ---- Tool output ----
    parser.add_argument(
        "--sort-tools", dest="sort_tools", action="store_true", default=True,
        help="Sort tools alphabetically (default: on)",
    )
    parser.add_argument(
        "--no-sort-tools", dest="sort_tools", action="store_false",
    )
    parser.add_argument(
        "--trace", action="store_true",
        help="Print all tools as JSON and exit (debug)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose stderr logging",
    )
    parser.add_argument(
        "--verbose-errors", action="store_true",
        help="Include HTTP status / URL / body in error responses",
    )
    parser.add_argument(
        "--no-legacy-dates", action="store_true",
        help="Disable automatic /Date(ms)/ -> ISO-8601 conversion",
    )
    parser.add_argument(
        "-c", "--claude-code-friendly", action="store_true",
        help="Strip $ prefix from OData param names (for Claude Code CLI)",
    )
    parser.add_argument(
        "--max-items", type=int, default=100,
        help="Hard cap on items returned per tool call (default: 100)",
    )
    parser.add_argument(
        "--max-response-size", type=int, default=5 * 1024 * 1024, metavar="BYTES",
        help="Hard byte cap on raw OData response body (default: 5 MB). "
             "Returns a structured error with filter hints when exceeded.",
    )

    args = parser.parse_args()

    # Re-load .env with the user-specified path (if different from default)
    if args.dotenv != ".env":
        _load_dotenv(args.dotenv)

    # ---- Validate mutually exclusive flags ----
    if args.read_only and args.read_only_but_functions:
        parser.error("--read-only and --read-only-but-functions are mutually exclusive")
    if args.enable and args.disable:
        parser.error("--enable and --disable are mutually exclusive")

    # ---- MCP token from file ----
    mcp_token = args.mcp_token
    if args.mcp_token_file and not mcp_token:
        try:
            mcp_token = open(args.mcp_token_file).read().strip()
        except OSError as exc:
            sys.stderr.write(f"[bridge] cannot read token file: {exc}\n")
            sys.exit(1)

    _init_btp_proxy()

    services = load_services(args.config, args)
    if not services:
        sys.stderr.write("[bridge] no services loaded - check your config\n")
        sys.exit(1)

    bridge = Bridge(services, sort_tools=args.sort_tools, verbose=args.verbose)

    # ---- Trace mode: dump tools and exit ----
    if args.trace:
        print_trace(bridge)
        sys.exit(0)

    # ---- Graceful shutdown ----
    def _shutdown(signum, _frame):
        sys.stderr.write(f"\n[bridge] signal {signum} - shutting down\n")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # ---- Transport selection ----
    if args.transport == "stdio":
        if args.verbose:
            sys.stderr.write(
                f"[bridge] stdio transport | {len(bridge._all_tools)} tools\n"
            )
        run_stdio(bridge, verbose=args.verbose)
        return

    # ---- HTTP transport ----
    MCPHandler = make_http_handler(
        bridge, mcp_token=mcp_token, passthrough=args.passthrough
    )
    MCPHandler.mcp_username = args.username
    MCPHandler.mcp_password = args.password

    _safe_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in _safe_hosts:
        if not args.security_bypass:
            sys.stderr.write(
                f"[bridge] SECURITY BLOCK: --host '{args.host}' would expose all\n"
                f"[bridge] OData credentials and MCP tools on the network.\n"
                f"[bridge] To allow this, re-run with:\n"
                f"[bridge]   --host {args.host} --i-am-security-expert\n"
            )
            sys.exit(1)
        sys.stderr.write(
            f"[bridge] warning: binding to {args.host} (security bypass active)\n"
        )

    server = ThreadingHTTPServer((args.host, args.port), MCPHandler)

    auth_mode = (
        f"bearer token"      if mcp_token
        else f"basic ({args.username})" if args.username
        else "open (no auth)"
    )
    sys.stderr.write(
        f"[bridge] listening on http://{args.host}:{args.port}/mcp\n"
        f"[bridge] auth: {auth_mode} | "
        f"tools: {len(bridge._all_tools)} | "
        f"transport: http\n"
    )

    if args.verbose:
        for svc in services:
            sys.stderr.write(
                f"[bridge]   {svc.alias}: "
                f"{len(svc.entity_sets)} entity sets, "
                f"{len(svc.actions)} actions | "
                f"legacy_dates={svc.legacy_dates} "
                f"claude_friendly={svc.claude_code_friendly} "
                f"max_items={svc.max_items}\n"
            )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[bridge] stopped\n")


if __name__ == "__main__":
    main()
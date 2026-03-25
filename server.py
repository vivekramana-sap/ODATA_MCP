#!/usr/bin/env python3
"""
JAM OData MCP Bridge
====================
Multi-service OData v4 → MCP bridge server.

- Auto-discovers entity sets and actions from $metadata
- Exposes CRUD + actions as MCP tools (namespaced per service alias)
- Supports passthrough auth (caller credentials forwarded to OData)
- CORS-enabled for Copilot Studio and browser-based MCP clients
- Pure Python stdlib — no external dependencies

Usage (local):
    python3 server.py --config services.json --port 7777

Usage (BTP CF):
    Set env vars PORT, ODATA_USERNAME, ODATA_PASSWORD, MCP_USERNAME, MCP_PASSWORD
    python3 server.py --config services.json --passthrough

Config (services.json):
    [
      {
        "alias": "ean_e1",
        "url": "http://host/sap/opu/odata4/.../",
        "username": "${ODATA_USERNAME}",
        "password": "${ODATA_PASSWORD}",
        "passthrough": true
      }
    ]
"""

import argparse
import base64
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ---------------------------------------------------------------------------
# BTP Connectivity proxy (on-premise access from CF)
# ---------------------------------------------------------------------------

_BTP_PROXY_URL: str = ""
_BTP_PROXY_TOKEN: str = ""


def _init_btp_proxy():
    """Read VCAP_SERVICES and obtain a proxy JWT for the Connectivity service."""
    global _BTP_PROXY_URL, _BTP_PROXY_TOKEN
    vcap_raw = os.environ.get("VCAP_SERVICES", "")
    if not vcap_raw:
        return
    try:
        vcap = json.loads(vcap_raw)
        conn = vcap.get("connectivity", [{}])[0].get("credentials", {})
        host = conn.get("onpremise_proxy_host", "")
        port = conn.get("onpremise_proxy_port", "20003")
        clientid = conn.get("clientid", "")
        clientsecret = conn.get("clientsecret", "")
        token_url = conn.get("token_service_url", "")
        if not all([host, clientid, clientsecret, token_url]):
            sys.stderr.write("[bridge] BTP connectivity: missing credentials in VCAP_SERVICES\n")
            return
        # Normalize token URL
        if not token_url.endswith("/oauth/token"):
            token_url = token_url.rstrip("/") + "/oauth/token"
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        auth = base64.b64encode(f"{clientid}:{clientsecret}".encode()).decode()
        req = urllib.request.Request(
            token_url, data=data,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            _BTP_PROXY_TOKEN = json.loads(r.read())["access_token"]
        _BTP_PROXY_URL = f"http://{host}:{port}"
        sys.stderr.write(f"[bridge] BTP connectivity proxy: {_BTP_PROXY_URL}\n")
    except Exception as e:
        sys.stderr.write(f"[bridge] BTP connectivity init failed: {e}\n")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"
HTTP_TIMEOUT = 30

EDM_TO_JSON = {
    "Edm.String": "string",
    "Edm.Int16": "integer", "Edm.Int32": "integer", "Edm.Int64": "integer",
    "Edm.Decimal": "number", "Edm.Double": "number", "Edm.Single": "number",
    "Edm.Boolean": "boolean",
    "Edm.Date": "string", "Edm.DateTimeOffset": "string", "Edm.TimeOfDay": "string",
    "Edm.Guid": "string", "Edm.Binary": "string",
    "Edm.Byte": "integer", "Edm.SByte": "integer",
}


def edm_to_json(edm_type: str) -> str:
    return EDM_TO_JSON.get(edm_type, "string")


def expand_env(value: str) -> str:
    """Expand ${VAR} placeholders in config strings."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------------------
# Threading HTTP Server
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# ODataService — one configured OData endpoint
# ---------------------------------------------------------------------------

class ODataService:
    def __init__(self, alias: str, url: str, username: str = "",
                 password: str = "", passthrough: bool = False,
                 include: list = None, readonly: bool = False,
                 include_actions: list = None):
        self.alias = alias
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.passthrough = passthrough  # forward caller's credentials to OData
        self.readonly = readonly        # skip create/update/delete tools
        self.include_actions = set(include_actions) if include_actions else None

        self.entity_sets: dict[str, dict] = {}
        self.actions: list[dict] = []
        self.schema_ns = ""

        # Bootstrap opener (service credentials) for metadata + fallback
        self._csrf_token = ""
        self._bootstrap_opener = self._make_opener(username, password)
        self._load_metadata()

        # Apply entity set whitelist after metadata is loaded
        if include:
            self.entity_sets = {k: v for k, v in self.entity_sets.items()
                                if k in include}

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _make_opener(self, username: str = "", password: str = "",
                     auth_header: str = "") -> urllib.request.OpenerDirector:
        handlers: list = [urllib.request.HTTPCookieProcessor(CookieJar())]

        # BTP on-premise proxy
        if _BTP_PROXY_URL:
            class _ProxyAuth(urllib.request.BaseHandler):
                handler_order = 490

                def http_request(self, req):
                    req.add_unredirected_header("Proxy-Authorization",
                                                f"Bearer {_BTP_PROXY_TOKEN}")
                    return req

                https_request = http_request

            handlers.append(urllib.request.ProxyHandler(
                {"http": _BTP_PROXY_URL, "https": _BTP_PROXY_URL}
            ))
            handlers.append(_ProxyAuth())

        opener = urllib.request.build_opener(*handlers)

        if auth_header:
            av = auth_header
        elif username:
            av = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        else:
            av = ""

        if av:
            class _Auth(urllib.request.BaseHandler):
                def http_request(self, req):
                    req.add_unredirected_header("Authorization", av)
                    return req
                https_request = http_request
            opener.add_handler(_Auth())
        return opener

    def _opener(self, auth_header: str = "") -> urllib.request.OpenerDirector:
        if self.passthrough and auth_header:
            return self._make_opener(auth_header=auth_header)
        return self._bootstrap_opener

    def _open(self, req, auth_header: str = ""):
        return self._opener(auth_header).open(req, timeout=HTTP_TIMEOUT)

    def _fetch_csrf(self, opener: urllib.request.OpenerDirector) -> str:
        """Fetch a CSRF token using the given opener (must be same session as the write request)."""
        first_es = next(iter(self.entity_sets), None)
        url = f"{self.url}/{first_es}?$top=0" if first_es else self.url
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "x-csrf-token": "Fetch"}
        )
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as r:
                token = r.headers.get("x-csrf-token", "")
                if token and not self.passthrough:
                    self._csrf_token = token
                return token or self._csrf_token
        except Exception:
            return self._csrf_token

    def _request(self, method: str, url: str, body: dict = None,
                 auth_header: str = "", _retry: bool = True):
        # Single opener per request so CSRF fetch and write share the same session/cookie
        if self.passthrough and auth_header:
            opener = self._make_opener(auth_header=auth_header)
        else:
            opener = self._bootstrap_opener

        csrf = ""
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            csrf = (self._fetch_csrf(opener) if self.passthrough
                    else self._csrf_token or self._fetch_csrf(opener))

        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if csrf:
            headers["x-csrf-token"] = csrf

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as r:
                raw = r.read().decode()
                result = json.loads(raw) if raw.strip() else {"status": "success"}
                sap_msg = r.headers.get("sap-messages", "")
                if sap_msg:
                    try:
                        result["sap_messages"] = json.loads(sap_msg)
                    except Exception:
                        pass
                return result
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            if e.code == 407 and _retry:
                sys.stderr.write("[bridge] Proxy token expired (407) — refreshing BTP token\n")
                _init_btp_proxy()
                return self._request(method, url, body, auth_header, _retry=False)
            if e.code == 403 and "csrf" in raw.lower() and _retry:
                self._csrf_token = ""
                return self._request(method, url, body, auth_header, _retry=False)
            try:
                return json.loads(raw)
            except Exception:
                return {"error": f"HTTP {e.code}: {raw}"}

    # ------------------------------------------------------------------ #
    # Metadata discovery                                                   #
    # ------------------------------------------------------------------ #

    def _load_metadata(self):
        req = urllib.request.Request(
            f"{self.url}/$metadata", headers={"Accept": "application/xml"}
        )
        with self._open(req) as r:
            root = ET.fromstring(r.read())

        # Auto-detect EDM namespace (OData v4 or v2)
        ns = EDM_NS
        for elem in root.iter():
            if "}" in elem.tag:
                candidate = elem.tag[1:elem.tag.index("}")]
                local = elem.tag[elem.tag.index("}") + 1:]
                if local == "Schema":
                    ns = candidate
                    break

        for schema in root.iter(f"{{{ns}}}Schema"):
            self.schema_ns = schema.get("Namespace", "")
            break

        # Entity types
        entity_types: dict[str, dict] = {}
        for et in root.iter(f"{{{ns}}}EntityType"):
            name = et.get("Name", "")
            keys = [p.get("Name") for p in et.findall(
                f"{{{ns}}}Key/{{{ns}}}PropertyRef")]
            props = {
                p.get("Name", ""): {
                    "type": p.get("Type", "Edm.String"),
                    "nullable": p.get("Nullable", "true").lower() != "false",
                }
                for p in et.findall(f"{{{ns}}}Property")
            }
            entity_types[name] = {"keys": keys, "props": props}

        # Entity sets
        et_to_es: dict[str, str] = {}
        for container in root.iter(f"{{{ns}}}EntityContainer"):
            for es in container.findall(f"{{{ns}}}EntitySet"):
                es_name = es.get("Name", "")
                et_local = es.get("EntityType", "").split(".")[-1]
                et_data = entity_types.get(et_local, {"keys": [], "props": {}})
                self.entity_sets[es_name] = {
                    "keys": et_data["keys"],
                    "props": et_data["props"],
                }
                et_to_es[et_local] = es_name

            # FunctionImports (OData v2 style actions)
            for fi in container.findall(f"{{{ns}}}FunctionImport"):
                params = []
                for p in fi.findall(f"{{{ns}}}Parameter"):
                    params.append({
                        "name": p.get("Name", ""),
                        "type": p.get("Type", "Edm.String"),
                        "required": p.get("Nullable", "true").lower() == "false",
                    })
                self.actions.append({
                    "name": fi.get("Name", ""),
                    "is_bound": False,
                    "is_collection_bound": False,
                    "entity_set": fi.get("EntitySet", ""),
                    "params": params,
                })

        # Actions (OData v4)
        for action in root.iter(f"{{{ns}}}Action"):
            is_bound = action.get("IsBound", "false").lower() == "true"
            params, binding_type, is_collection = [], "", False

            for i, p in enumerate(action.findall(f"{{{ns}}}Parameter")):
                ptype = p.get("Type", "Edm.String")
                if is_bound and i == 0:
                    binding_type, is_collection = ptype, ptype.startswith("Collection(")
                    continue
                params.append({
                    "name": p.get("Name", ""),
                    "type": ptype,
                    "required": p.get("Nullable", "true").lower() == "false",
                })

            entity_set = ""
            if is_bound and binding_type:
                bt = binding_type.replace("Collection(", "").rstrip(")")
                entity_set = et_to_es.get(bt.split(".")[-1], "")

            self.actions.append({
                "name": action.get("Name", ""),
                "is_bound": is_bound,
                "is_collection_bound": is_collection,
                "entity_set": entity_set,
                "params": params,
            })

        # Apply action filter
        if self.include_actions is not None:
            self.actions = [a for a in self.actions if a["name"] in self.include_actions]

    # ------------------------------------------------------------------ #
    # OData operations                                                     #
    # ------------------------------------------------------------------ #

    def filter(self, entity_set: str, args: dict, auth: str = "") -> dict:
        qs = {}
        for k in ("filter", "select", "orderby", "expand"):
            if args.get(k):
                qs[f"${k}"] = args[k]
        if args.get("top") is not None:
            qs["$top"] = str(args["top"])
        if args.get("skip") is not None:
            qs["$skip"] = str(args["skip"])
        if args.get("count"):
            qs["$count"] = "true"
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in qs.items())
        return self._request("GET", f"{self.url}/{entity_set}" + (f"?{q}" if q else ""),
                             auth_header=auth)

    def count(self, entity_set: str, filter_expr: str = "", auth: str = "") -> dict:
        url = f"{self.url}/{entity_set}/$count"
        if filter_expr:
            url += f"?$filter={urllib.parse.quote(filter_expr)}"
        return self._request("GET", url, auth_header=auth)

    def get(self, entity_set: str, key: str, args: dict, auth: str = "") -> dict:
        qs = {f"${k}": args[k] for k in ("select", "expand") if args.get(k)}
        q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in qs.items())
        return self._request("GET", f"{self.url}/{entity_set}({key})" + (f"?{q}" if q else ""),
                             auth_header=auth)

    def create(self, entity_set: str, body: dict, auth: str = "") -> dict:
        return self._request("POST", f"{self.url}/{entity_set}", body, auth_header=auth)

    def update(self, entity_set: str, key: str, body: dict,
               method: str = "PATCH", auth: str = "") -> dict:
        return self._request(method, f"{self.url}/{entity_set}({key})", body, auth_header=auth)

    def delete(self, entity_set: str, key: str, auth: str = "") -> dict:
        return self._request("DELETE", f"{self.url}/{entity_set}({key})", auth_header=auth)

    def action(self, action_name: str, args: dict, auth: str = "") -> dict:
        act = next((a for a in self.actions if a["name"] == action_name), None)
        if not act:
            return {"error": f"Action '{action_name}' not found"}

        qualified = f"{self.schema_ns}.{action_name}" if self.schema_ns else action_name
        es = act["entity_set"]

        if act["is_bound"] and es:
            if act["is_collection_bound"]:
                url = f"{self.url}/{es}/{qualified}"
            else:
                key = args.pop("_entity_key", "")
                url = f"{self.url}/{es}({key})/{qualified}"
        else:
            url = f"{self.url}/{qualified}"

        body = {k: v for k, v in args.items() if not k.startswith("_")}
        return self._request("POST", url, body, auth_header=auth)


# ---------------------------------------------------------------------------
# Bridge — manages multiple services, generates MCP tool schemas
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, services: list[ODataService]):
        self.services: dict[str, ODataService] = {s.alias: s for s in services}

    def tools(self) -> list[dict]:
        result = []
        for alias, svc in self.services.items():
            for es_name, es in svc.entity_sets.items():
                keys = es["keys"]
                props = es["props"]
                key_schema = {k: {"type": edm_to_json(props.get(k, {}).get("type", "Edm.String")),
                                   "description": f"Key: {k}"} for k in keys}
                all_schema = {n: {"type": edm_to_json(m["type"]), "description": n}
                              for n, m in props.items()}
                non_key_schema = {n: v for n, v in all_schema.items() if n not in keys}

                field_list = ", ".join(props.keys())
                filter_desc = (f"OData filter expression, e.g. \"{keys[0]} eq 'value'\". "
                               f"Fields: {field_list}") if keys else f"OData filter expression. Fields: {field_list}"

                es_tools = [
                    self._tool(f"{alias}_filter_{es_name}",
                               f"[{alias}] List/filter {es_name} — use filter param to narrow results, e.g. \"{keys[0]} eq '1002'\"" if keys else f"[{alias}] List/filter {es_name}",
                               {"filter": {"type": "string", "description": filter_desc},
                                "select": {"type": "string", "description": f"Comma-separated fields to return. Available: {field_list}"},
                                "orderby": {"type": "string", "description": "Field name to sort by, e.g. 'SupplierName asc'"},
                                "expand": {"type": "string", "description": "Navigation properties to expand"},
                                "top": {"type": "integer", "description": "Max number of results to return"},
                                "skip": {"type": "integer", "description": "Number of results to skip (for pagination)"},
                                "count": {"type": "boolean", "description": "Include total count in response"}}),
                    self._tool(f"{alias}_count_{es_name}",
                               f"[{alias}] Count {es_name}",
                               {"filter": {"type": "string", "description": filter_desc}}),
                    self._tool(f"{alias}_get_{es_name}",
                               f"[{alias}] Get single {es_name} by key",
                               {**key_schema,
                                "select": {"type": "string", "description": f"Fields to return. Available: {field_list}"},
                                "expand": {"type": "string", "description": "Navigation properties to expand"}},
                               required=keys),
                ]
                if not svc.readonly:
                    es_tools += [
                        self._tool(f"{alias}_create_{es_name}",
                                   f"[{alias}] Create {es_name}",
                                   non_key_schema),
                        self._tool(f"{alias}_update_{es_name}",
                                   f"[{alias}] Update {es_name} (PATCH)",
                                   {**all_schema, "_key": {"type": "string",
                                    "description": f"Key predicate, e.g. {keys[0]}='X'" if keys else "Key predicate"}},
                                   required=["_key"]),
                        self._tool(f"{alias}_delete_{es_name}",
                                   f"[{alias}] Delete {es_name}",
                                   {"_key": {"type": "string", "description": "Key predicate"}},
                                   required=["_key"]),
                    ]
                result += es_tools

            for act in svc.actions:
                props_schema = {}
                required = []
                if act["is_bound"] and not act["is_collection_bound"] and act["entity_set"]:
                    props_schema["_entity_key"] = {"type": "string",
                                                   "description": "Key predicate"}
                    required.append("_entity_key")
                for p in act["params"]:
                    props_schema[p["name"]] = {"type": edm_to_json(p["type"]),
                                               "description": f"({p['type']})"}
                    if p["required"]:
                        required.append(p["name"])

                es = act["entity_set"]
                if act["is_bound"]:
                    desc = (f"[{alias}] Action '{act['name']}' on "
                            f"{'collection' if act['is_collection_bound'] else 'entity'} {es}")
                else:
                    desc = f"[{alias}] Unbound action '{act['name']}'"

                result.append(self._tool(f"{alias}_action_{act['name']}", desc,
                                         props_schema, required=required))
        return result

    @staticmethod
    def _tool(name: str, desc: str, props: dict, required: list = None) -> dict:
        # Allow props to be either {"name": "type_string"} or {"name": {schema_dict}}
        schema_props = {}
        for k, v in props.items():
            if isinstance(v, str):
                schema_props[k] = {"type": v, "description": k}
            else:
                schema_props[k] = v
        schema = {"type": "object", "properties": schema_props}
        if required:
            schema["required"] = required
        return {"name": name, "description": desc, "inputSchema": schema}

    def call(self, name: str, args: dict, auth: str = "") -> dict:
        for alias, svc in self.services.items():
            if not name.startswith(alias + "_"):
                continue
            rest = name[len(alias) + 1:]
            ah = auth if svc.passthrough else ""

            if rest.startswith("action_"):
                return svc.action(rest[7:], dict(args), auth=ah)

            for op in ("filter", "count", "get", "create", "update", "delete"):
                if not rest.startswith(op + "_"):
                    continue
                es_name = rest[len(op) + 1:]
                if es_name not in svc.entity_sets:
                    return {"error": f"Unknown entity set: {es_name}"}
                args = dict(args)
                if op == "filter":
                    return svc.filter(es_name, args, auth=ah)
                if op == "count":
                    return svc.count(es_name, args.get("filter", ""), auth=ah)
                if op == "get":
                    key_parts = []
                    for k in svc.entity_sets[es_name]["keys"]:
                        v = args.pop(k, "")
                        key_parts.append(f"{k}='{v}'" if isinstance(v, str) else f"{k}={v}")
                    return svc.get(es_name, ",".join(key_parts), args, auth=ah)
                if op == "create":
                    return svc.create(es_name, args, auth=ah)
                if op == "update":
                    return svc.update(es_name, args.pop("_key", ""), args, auth=ah)
                if op == "delete":
                    return svc.delete(es_name, args.pop("_key", ""), auth=ah)

        return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# MCP HTTP Handler
# ---------------------------------------------------------------------------

class MCPHandler(BaseHTTPRequestHandler):
    bridge: Bridge = None
    mcp_username: str = ""
    mcp_password: str = ""
    passthrough: bool = False

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.address_string()}] {fmt % args}\n")

    # -- Auth ---------------------------------------------------------------

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if self.passthrough:
            return bool(auth)
        if not self.mcp_username:
            return True
        if not auth.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
            return u == self.mcp_username and p == self.mcp_password
        except Exception:
            return False

    # -- CORS ---------------------------------------------------------------

    def _cors(self):
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, Mcp-Session-Id")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Max-Age", "86400")

    # -- Response helpers ---------------------------------------------------

    def _json(self, status: int, body: dict, session_id: str = ""):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _no_content(self, status: int):
        self.send_response(status)
        self._cors()
        self.end_headers()

    # -- HTTP verbs ---------------------------------------------------------

    def do_OPTIONS(self):
        self._no_content(204)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._json(200, {"status": "ok",
                             "services": list(self.bridge.services.keys()),
                             "tools": len(self.bridge.tools())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/mcp", "/mcp/"):
            self._json(404, {"error": "not found"})
            return

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="odata-mcp-bridge"')
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        # Notifications (no id) → 202, no body
        if "id" not in req:
            self._no_content(202)
            return

        auth = self.headers.get("Authorization", "")
        result, session_id = self._dispatch(req, auth)
        self._json(200, result, session_id=session_id)

    # -- MCP dispatch -------------------------------------------------------

    def _dispatch(self, req: dict, auth: str) -> tuple[dict, str]:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})
        session_id = ""

        if method == "initialize":
            session_id = str(uuid.uuid4())
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jam-odata-mcp-bridge", "version": "1.0.0"},
                },
            }, session_id

        if method == "tools/list":
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": self.bridge.tools()},
            }, session_id

        if method == "tools/call":
            result = self.bridge.call(
                params.get("name", ""),
                dict(params.get("arguments", {})),
                auth=auth,
            )
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }, session_id

        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }, session_id


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> list[ODataService]:
    with open(path) as f:
        items = json.load(f)

    services = []
    for item in items:
        alias = item["alias"]
        url = expand_env(item["url"])
        username = expand_env(item.get("username", ""))
        password = expand_env(item.get("password", ""))
        passthrough = item.get("passthrough", False)

        include = item.get("include") or None
        readonly = item.get("readonly", False)

        sys.stderr.write(f"[bridge] Loading '{alias}' {url}"
                         f"{' [passthrough]' if passthrough else ''}"
                         f"{' [readonly]' if readonly else ''}\n")
        try:
            include_actions = item.get("include_actions", None)
            svc = ODataService(alias, url, username, password, passthrough,
                               include=include, readonly=readonly,
                               include_actions=include_actions)
            sys.stderr.write(f"[bridge]   entity sets : {list(svc.entity_sets)}\n")
            sys.stderr.write(f"[bridge]   actions     : {[a['name'] for a in svc.actions]}\n")
            services.append(svc)
        except Exception as e:
            sys.stderr.write(f"[bridge]   ERROR: {e}\n")
    return services


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="JAM OData MCP Bridge")
    parser.add_argument("--config", default=os.environ.get("CONFIG_FILE", "services.json"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7777)))
    parser.add_argument("--username", default=os.environ.get("MCP_USERNAME", ""),
                        help="MCP Basic Auth username")
    parser.add_argument("--password", default=os.environ.get("MCP_PASSWORD", ""),
                        help="MCP Basic Auth password")
    parser.add_argument("--passthrough", action="store_true",
                        default=os.environ.get("MCP_PASSTHROUGH", "").lower() == "true",
                        help="Forward caller credentials to OData (end-user auth mode)")
    args = parser.parse_args()

    _init_btp_proxy()

    services = load_config(args.config)
    if not services:
        sys.stderr.write("[bridge] No services loaded — check config and credentials\n")
        sys.exit(1)

    bridge = Bridge(services)
    sys.stderr.write(f"[bridge] {len(services)} service(s), {len(bridge.tools())} tools\n")

    MCPHandler.bridge = bridge
    MCPHandler.mcp_username = args.username
    MCPHandler.mcp_password = args.password
    MCPHandler.passthrough = args.passthrough

    server = ThreadingHTTPServer(("0.0.0.0", args.port), MCPHandler)

    mode = ("passthrough" if args.passthrough
            else f"service credentials ({args.username})" if args.username
            else "no auth (open)")
    sys.stderr.write(f"[bridge] http://0.0.0.0:{args.port}/mcp  |  auth: {mode}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[bridge] stopped\n")


if __name__ == "__main__":
    main()

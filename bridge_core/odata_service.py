"""
ODataService — one configured OData endpoint.

Handles metadata parsing, CRUD operations, action/function calls,
CSRF token management, and BTP connectivity proxy integration.
"""

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from .constants import EDM_NS, HTTP_TIMEOUT, _GUID_RE
from .helpers import (
    edm_to_json,
    convert_legacy_dates,
    OpFilter,
    matches_patterns,
    load_cookies_from_file,
    parse_cookie_string,
)
from . import auth as _auth
from .auth import _get_btp_token


# ---------------------------------------------------------------------------
# Threading HTTP Server
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# ODataService
# ---------------------------------------------------------------------------

class ODataService:
    def __init__(
        self,
        alias:                  str,
        url:                    str,
        username:               str  = "",
        password:               str  = "",
        passthrough:            bool = False,
        passthrough_header:     str  = "",
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
        self.passthrough          = passthrough
        self.passthrough_header   = passthrough_header
        self.legacy_dates         = legacy_dates
        self.claude_code_friendly = claude_code_friendly
        self.verbose_errors       = verbose_errors
        self.max_items            = max_items
        self.max_response_size    = max_response_size

        self.op_filter = OpFilter(
            enable_ops             = enable_ops,
            disable_ops            = disable_ops,
            readonly               = readonly,
            readonly_but_functions = readonly_but_functions,
        )
        self.readonly        = readonly
        self.include_actions = set(include_actions) if include_actions else None
        self.default_top     = default_top
        self.max_top         = max_top

        self.entity_sets: dict[str, dict] = {}
        self.actions:     list[dict]      = []
        self.schema_ns                    = ""
        self.odata_version                = ""

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
        username:      str = "",
        password:      str = "",
        auth_header:   str = "",
        auth_hdr_name: str = "Authorization",
    ) -> urllib.request.OpenerDirector:

        handlers: list = [urllib.request.HTTPCookieProcessor(CookieJar())]

        if _auth._BTP_PROXY_URL:
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
                    {"http": _auth._BTP_PROXY_URL, "https": _auth._BTP_PROXY_URL}
                )
            )
            handlers.append(_ProxyAuth())

        opener = urllib.request.build_opener(*handlers)

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
            _av           = av
            _cookies      = extra_cookies
            _auth_hdr_name = auth_hdr_name
            _auth_header_name = _auth_hdr_name

            class _Auth(urllib.request.BaseHandler):
                def http_request(self, req):
                    if _av:
                        req.add_unredirected_header(_auth_header_name, _av)
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
            hdr_name = self.passthrough_header or "Authorization"
            return self._make_opener(
                auth_header   = auth_header,
                auth_hdr_name = hdr_name,
            )
        return self._bootstrap_opener

    def _open(self, req, auth_header: str = ""):
        return self._opener(auth_header).open(req, timeout=HTTP_TIMEOUT)

    def _fetch_csrf(self, opener: urllib.request.OpenerDirector) -> str:
        url = f"{self.url}/$metadata"
        req = urllib.request.Request(url, headers={"x-csrf-token": "Fetch"})
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as r:
                token = r.headers.get("x-csrf-token", "")
                sys.stderr.write(f"[bridge] {self.alias}: CSRF token fetch {'ok' if token else 'empty (no token returned)'}\n")
                if not token:
                    raise RuntimeError(
                        f"CSRF token fetch returned empty for {self.alias}. "
                        "The OData server did not provide an x-csrf-token header."
                    )
                return token
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"CSRF token fetch failed for {self.alias}: {type(exc).__name__}"
            ) from exc

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

        is_v4 = detected_ns == "http://docs.oasis-open.org/odata/ns/edm"
        self.odata_version = "4" if is_v4 else "2"

        # ---- EntityType map ----
        SAP_NS = "http://www.sap.com/Protocols/SAPData"

        def _is_internal_prop(pname: str, ptype: str) -> bool:
            if pname.startswith("__") or pname.startswith("SAP__"):
                return True
            if not ptype.startswith("Edm.") and not ptype.startswith("Collection(Edm."):
                return True
            return False

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
                label    = prop.get(f"{{{SAP_NS}}}label", "") or prop.get("sap:label", "")
                props[pname] = {
                    "type":       edm_to_json(ptype),
                    "edm_type":   ptype,
                    "nullable":   nullable,
                    "label":      label,
                    "internal":   _is_internal_prop(pname, ptype),
                }
            nav_props_list = []
            for npelem in et.findall("edm:NavigationProperty", ns):
                nav_info = {"name": npelem.get("Name", "")}
                if is_v4:
                    nav_info["type"]     = npelem.get("Type", "")
                    nav_info["partner"]  = npelem.get("Partner", "")
                    nav_info["nullable"] = npelem.get("Nullable", "true").lower() != "false"
                else:
                    nav_info["relationship"] = npelem.get("Relationship", "")
                    nav_info["to_role"]      = npelem.get("ToRole", "")
                    nav_info["from_role"]    = npelem.get("FromRole", "")
                nav_props_list.append(nav_info)

            entity_types[et_name] = {
                "keys":      keys,
                "props":     props,
                "nav_props": [np["name"] for np in nav_props_list],
                "nav_props_detail": nav_props_list,
            }

        # ---- EntitySet -> EntityType mapping + SAP capability attributes ----
        type_to_set: dict = {}
        for ec in schema.findall(".//edm:EntitySet", ns):
            es_name = ec.get("Name", "")
            et_name = ec.get("EntityType", "").split(".")[-1]
            if et_name not in entity_types:
                continue

            es_data = dict(entity_types[et_name])
            type_to_set[et_name] = es_name

            sap_creatable  = ec.get(f"{{{SAP_NS}}}creatable",  "") or ec.get("sap:creatable",  "")
            sap_updatable  = ec.get(f"{{{SAP_NS}}}updatable",  "") or ec.get("sap:updatable",  "")
            sap_deletable  = ec.get(f"{{{SAP_NS}}}deletable",  "") or ec.get("sap:deletable",  "")
            sap_searchable = ec.get(f"{{{SAP_NS}}}searchable", "") or ec.get("sap:searchable", "")
            sap_pageable   = ec.get(f"{{{SAP_NS}}}pageable",   "") or ec.get("sap:pageable",   "")

            if is_v4:
                caps = {
                    "creatable":  True,
                    "updatable":  True,
                    "deletable":  True,
                    "searchable": False,
                    "pageable":   True,
                }
                CAP_NS = "http://docs.oasis-open.org/odata/ns/edm"
                for ann in ec.findall(f"{{{CAP_NS}}}Annotation"):
                    term = ann.get("Term", "")
                    if term.endswith("SearchRestrictions") or term == "Org.OData.Capabilities.V1.SearchRestrictions":
                        rec = ann.find(f"{{{CAP_NS}}}Record")
                        if rec is not None:
                            for pv in rec.findall(f"{{{CAP_NS}}}PropertyValue"):
                                if pv.get("Property") == "Searchable":
                                    caps["searchable"] = pv.get("Bool", "false").lower() == "true"
                    elif term.endswith("InsertRestrictions") or term == "Org.OData.Capabilities.V1.InsertRestrictions":
                        rec = ann.find(f"{{{CAP_NS}}}Record")
                        if rec is not None:
                            for pv in rec.findall(f"{{{CAP_NS}}}PropertyValue"):
                                if pv.get("Property") == "Insertable":
                                    caps["creatable"] = pv.get("Bool", "true").lower() != "false"
                    elif term.endswith("UpdateRestrictions") or term == "Org.OData.Capabilities.V1.UpdateRestrictions":
                        rec = ann.find(f"{{{CAP_NS}}}Record")
                        if rec is not None:
                            for pv in rec.findall(f"{{{CAP_NS}}}PropertyValue"):
                                if pv.get("Property") == "Updatable":
                                    caps["updatable"] = pv.get("Bool", "true").lower() != "false"
                    elif term.endswith("DeleteRestrictions") or term == "Org.OData.Capabilities.V1.DeleteRestrictions":
                        rec = ann.find(f"{{{CAP_NS}}}Record")
                        if rec is not None:
                            for pv in rec.findall(f"{{{CAP_NS}}}PropertyValue"):
                                if pv.get("Property") == "Deletable":
                                    caps["deletable"] = pv.get("Bool", "true").lower() != "false"
            else:
                caps = {
                    "creatable":  sap_creatable  != "false",
                    "updatable":  sap_updatable  != "false",
                    "deletable":  sap_deletable  != "false",
                    "searchable": sap_searchable == "true",
                    "pageable":   sap_pageable   != "false",
                }

            es_data["capabilities"] = caps
            self.entity_sets[es_name] = es_data

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
            binding_done  = False

            for param in node.findall("edm:Parameter", ns):
                pname = param.get("Name", "")
                ptype = param.get("Type", "Edm.String")
                if is_bound and not binding_done:
                    binding_done = True
                    if ptype.startswith("Collection("):
                        is_collection = True
                        inner_type = ptype[11:-1].split(".")[-1]
                        entity_set = type_to_set.get(inner_type, inner_type)
                    continue
                params.append({"name": pname, "type": edm_to_json(ptype), "edm_type": ptype})

            self.actions.append({
                "name":                a_name,
                "is_bound":            is_bound,
                "is_collection_bound": is_collection,
                "entity_set":          entity_set,
                "params":              params,
                "http_method":         "POST" if node.tag.endswith("Action") else "GET",
            })

        # ---- FunctionImports (OData v2) ----
        M_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
        for fi_node in schema.findall(".//edm:FunctionImport", ns):
            fi_name    = fi_node.get("Name", "")
            if any(a["name"] == fi_name for a in self.actions):
                continue
            return_type = fi_node.get("ReturnType", "")
            http_method = (
                fi_node.get(f"{{{M_NS}}}HttpMethod", "")
                or fi_node.get("m:HttpMethod", "")
                or "GET"
            )
            params = []
            for param in fi_node.findall("edm:Parameter", ns):
                pname = param.get("Name", "")
                ptype = param.get("Type", "Edm.String")
                pmode = param.get("Mode", "In")
                if pmode in ("In", "InOut"):
                    params.append({"name": pname, "type": edm_to_json(ptype), "edm_type": ptype})

            self.actions.append({
                "name":                fi_name,
                "is_bound":            False,
                "is_collection_bound": False,
                "entity_set":          "",
                "params":              params,
                "http_method":         http_method,
                "return_type":         return_type,
                "is_v2_function":      True,
            })

        if self.include_actions is not None:
            self.actions = [
                a for a in self.actions
                if a["name"] in self.include_actions
            ]

        sys.stderr.write(
            f"[bridge] {self.alias}: "
            f"OData v{self.odata_version}, "
            f"{len(self.entity_sets)} entity sets, "
            f"{len(self.actions)} actions/functions\n"
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
        opener:        urllib.request.OpenerDirector = None,
    ) -> dict:
        if self.odata_version == "2":
            headers = {"Accept": "application/json"}
        else:
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
            _opener = opener or self._opener(auth_header)
            with _opener.open(req, timeout=HTTP_TIMEOUT) as r:
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
            sys.stderr.write(f"[bridge] {self.alias}: {method} {url} → HTTP {exc.code}\n")
            if self.verbose_errors:
                return {
                    "error":       f"HTTP {exc.code}: {exc.reason}",
                    "http_status": exc.code,
                    "detail":      body_text,
                }
            return {"error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:
            sys.stderr.write(f"[bridge] {self.alias}: {method} error: {exc}\n")
            if self.verbose_errors:
                return {"error": str(exc)}
            return {"error": "Internal error processing OData request"}

    def _v2_params(self) -> dict:
        """Return OData v2-specific query parameters."""
        if self.odata_version != "2":
            return {}
        return {"$format": "json", "$inlinecount": "allpages"}

    @staticmethod
    def _normalize_v2_response(result: dict) -> dict:
        """Normalise OData v2 response shape to v4 style.

        v2 returns {"d": {"results": [...], "__count": "42"}}
        v4 returns {"value": [...], "@odata.count": 42}
        """
        d = result.get("d")
        if d is None:
            return result
        # Collection response
        if isinstance(d, dict) and "results" in d:
            out: dict = {"value": d["results"]}
            cnt = d.get("__count")
            if cnt is not None:
                try:
                    out["@odata.count"] = int(cnt)
                except (ValueError, TypeError):
                    pass
            nxt = d.get("__next")
            if nxt:
                out["@odata.nextLink"] = nxt
            return out
        # Single-entity response  {"d": {"BusinessPartner": "1", ...}}
        return d

    def _wrap_guid_key(self, entity_set: str, key_name: str, value) -> str:
        """For v2, wrap a GUID string value with guid'...' syntax."""
        if self.odata_version != "2" or not isinstance(value, str):
            return f"{key_name}='{value}'" if isinstance(value, str) else f"{key_name}={value}"
        edm_type = (
            self.entity_sets.get(entity_set, {})
            .get("props", {})
            .get(key_name, {})
            .get("edm_type", "")
        )
        if edm_type == "Edm.Guid" or _GUID_RE.match(value):
            return f"{key_name}=guid'{value}'"
        return f"{key_name}='{value}'"

    def filter(self, entity_set: str, args: dict, auth: str = "") -> dict:
        def _a(key: str):
            return args.get(f"${key}") or args.get(key)

        _top = _a("top")
        if _top is None and self.default_top:
            _top = self.default_top
        if self.max_top and _top is not None:
            _top = min(int(_top), self.max_top)

        params: dict = dict(self._v2_params())
        if _a("filter"):
            params["$filter"]  = _a("filter")
        if _top is not None:
            params["$top"]     = str(_top)
        if _a("skip") is not None:
            params["$skip"]    = str(_a("skip"))
        if _a("select"):
            params["$select"]  = _a("select")
        if _a("orderby"):
            params["$orderby"] = _a("orderby")
        if _a("expand"):
            params["$expand"]  = _a("expand")
        if _a("search"):
            params["$search"]  = _a("search")
        if args.get("count"):
            if self.odata_version == "2":
                params["$inlinecount"] = "allpages"
            else:
                params["$count"]   = "true"

        qs  = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in params.items()
        )
        url = f"{self.url}/{entity_set}" + (f"?{qs}" if qs else "")
        result = self._request("GET", url, auth_header=auth)
        if isinstance(result, dict) and "d" in result:
            result = self._normalize_v2_response(result)

        if isinstance(result, dict):
            items = result.get("value", [])
            top   = int(_top or 0)
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
        if self.odata_version == "2":
            # v2: use $inlinecount=allpages&$top=0 to get count without data
            params = {"$format": "json", "$inlinecount": "allpages", "$top": "0"}
            if filter_expr:
                params["$filter"] = filter_expr
            qs = "&".join(
                f"{k}={urllib.parse.quote(str(v), safe='')}"
                for k, v in params.items()
            )
            url = f"{self.url}/{entity_set}?{qs}"
            result = self._request("GET", url, auth_header=auth)
            if isinstance(result, dict) and "d" in result:
                result = self._normalize_v2_response(result)
            return result
        url = f"{self.url}/{entity_set}/$count"
        if filter_expr:
            url += f"?$filter={urllib.parse.quote(filter_expr, safe='')}"
        return self._request("GET", url, auth_header=auth)

    def search(self, entity_set: str, args: dict, auth: str = "") -> dict:
        """Full-text search on an entity set using $search."""
        search_term = args.get("$search") or args.get("search", "")
        if not search_term:
            return {"error": "Missing required parameter: search"}

        params: dict = dict(self._v2_params())
        params["$search"] = search_term
        if args.get("$select") or args.get("select"):
            params["$select"] = args.get("$select") or args.get("select")
        if args.get("$top") or args.get("top"):
            params["$top"] = str(args.get("$top") or args.get("top"))
        elif self.default_top:
            params["$top"] = str(self.default_top)

        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in params.items()
        )
        url = f"{self.url}/{entity_set}?{qs}"
        result = self._request("GET", url, auth_header=auth)
        if isinstance(result, dict) and "d" in result:
            result = self._normalize_v2_response(result)
        return result

    def get(self, entity_set: str, key: str, args: dict, auth: str = "") -> dict:
        qs_parts: dict = dict(self._v2_params())
        if args.get("$select"):
            qs_parts["$select"] = args["$select"]
        if args.get("$expand"):
            qs_parts["$expand"] = args["$expand"]
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in qs_parts.items()
        )
        url = f"{self.url}/{entity_set}({key})" + (f"?{qs}" if qs else "")
        result = self._request("GET", url, auth_header=auth)
        if isinstance(result, dict) and "d" in result:
            result = self._normalize_v2_response(result)
        return result

    def create(self, entity_set: str, body: dict, auth: str = "") -> dict:
        opener = self._bootstrap_opener
        csrf   = self._fetch_csrf(opener)
        return self._request(
            "POST",
            f"{self.url}/{entity_set}",
            body=body,
            extra_headers={"x-csrf-token": csrf},
            opener=opener,
        )

    def update(
        self, entity_set: str, key: str, body: dict,
        method: str = "PATCH", auth: str = "",
    ) -> dict:
        opener = self._bootstrap_opener
        csrf   = self._fetch_csrf(opener)
        return self._request(
            method,
            f"{self.url}/{entity_set}({key})",
            body=body,
            extra_headers={"x-csrf-token": csrf},
            opener=opener,
        )

    def delete(self, entity_set: str, key: str, auth: str = "") -> dict:
        opener = self._bootstrap_opener
        csrf   = self._fetch_csrf(opener)
        return self._request(
            "DELETE",
            f"{self.url}/{entity_set}({key})",
            extra_headers={"x-csrf-token": csrf},
            opener=opener,
        )

    def call_action(
        self, action_name: str, params: dict, auth: str = ""
    ) -> dict:
        opener = self._bootstrap_opener
        action_meta = next(
            (a for a in self.actions if a["name"] == action_name), {}
        )
        entity_set  = action_meta.get("entity_set", "")
        http_method = action_meta.get("http_method", "POST").upper()
        is_v2_func  = action_meta.get("is_v2_function", False)

        if is_v2_func:
            if http_method == "GET":
                qs_parts = []
                for k, v in params.items():
                    if isinstance(v, str):
                        qs_parts.append(f"{k}='{urllib.parse.quote(v, safe='')}'")
                    else:
                        qs_parts.append(f"{k}={v}")
                qs = ",".join(qs_parts)
                url = f"{self.url}/{action_name}"
                if qs:
                    url += f"?{qs}"
                return self._request("GET", url, auth_header=auth, opener=opener)
            else:
                csrf = self._fetch_csrf(opener)
                url = f"{self.url}/{action_name}"
                return self._request(
                    http_method, url, body=params,
                    extra_headers={"x-csrf-token": csrf} if csrf else None,
                    opener=opener,
                )

        csrf = self._fetch_csrf(opener)
        fqn  = (
            f"{self.schema_ns}.{action_name}" if self.schema_ns else action_name
        )
        url = (
            f"{self.url}/{entity_set}/{fqn}" if entity_set
            else f"{self.url}/{fqn}"
        )
        return self._request(
            http_method,
            url,
            body=params if http_method != "GET" else None,
            extra_headers={"x-csrf-token": csrf} if csrf else None,
            opener=opener,
        )

    # ------------------------------------------------------------------ #
    # Tool-name param helper                                               #
    # ------------------------------------------------------------------ #

    def _strip_dollar(self, name: str) -> str:
        """Strip $ prefix from OData system params."""
        if name.startswith("$"):
            return name[1:]
        return name

    @staticmethod
    def _safe_prop(name: str) -> str:
        """Sanitize OData property name to match ^[a-zA-Z0-9_.-]{1,64}$."""
        sanitized = re.sub(r'[^a-zA-Z0-9_.\-]', '_', name)[:64]
        return sanitized or "_"

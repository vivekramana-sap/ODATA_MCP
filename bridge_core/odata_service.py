"""
ODataService — one configured OData endpoint.

Handles metadata parsing, CRUD operations, action/function calls,
CSRF token management, and BTP connectivity proxy integration.
"""

import json
import sys
import urllib.parse        # used for v2 FunctionImport param quoting in call_action
import xml.etree.ElementTree as ET

import requests
from requests import Session

from .constants import EDM_NS, HTTP_TIMEOUT, _GUID_RE
from .helpers import (
    edm_to_json,
    convert_legacy_dates,
    OpFilter,
    matches_patterns,
    load_cookies_from_file,
    parse_cookie_string,
    safe_prop_name,
)
from . import auth as _auth


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
        cookie_file:            str  = "",
        cookie_string:          str  = "",
        verbose_errors:         bool = False,
        max_items:              int  = 100,
        max_response_size:      int  = 5 * 1024 * 1024,
        group:                  str  = "",
    ):
        self.alias                = alias
        self.group                = group
        self.url                  = url.rstrip("/")
        self.username             = username
        self.password             = password
        self.passthrough          = passthrough
        self.passthrough_header   = passthrough_header
        self.legacy_dates         = legacy_dates
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

        self._bootstrap_session     = self._make_session(username, password)
        self._load_metadata()

        if include:
            self.entity_sets = {
                k: v for k, v in self.entity_sets.items()
                if matches_patterns(k, include)
            }

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _make_session(
        self,
        username:      str = "",
        password:      str = "",
        auth_header:   str = "",
        auth_hdr_name: str = "Authorization",
    ) -> Session:
        """Build a requests.Session pre-configured with auth, cookies, and BTP proxy."""
        s = requests.Session()
        if _auth._BTP_PROXY_URL:
            s.proxies = {"http": _auth._BTP_PROXY_URL, "https": _auth._BTP_PROXY_URL}
        if auth_header:
            s.headers[auth_hdr_name] = auth_header
        elif username:
            s.auth = (username, password)
        if self._extra_cookies:
            s.cookies.update(self._extra_cookies)
        return s

    def _session_for(self, auth_header: str = "") -> Session:
        """Return the per-request session (passthrough) or the cached bootstrap session."""
        if auth_header and (
            self.passthrough
            or (auth_header.startswith("Bearer ") and _auth._XSUAA_INTROSPECT_URL)
        ):
            hdr_name = self.passthrough_header or "Authorization"
            return self._make_session(auth_header=auth_header, auth_hdr_name=hdr_name)
        return self._bootstrap_session

    def _btp_refresh(self, session: Session) -> None:
        """Refresh the BTP Proxy-Authorization header on the session before each call."""
        if _auth._BTP_PROXY_URL:
            session.headers["Proxy-Authorization"] = f"Bearer {_auth._get_btp_token()}"

    def _fetch_csrf(self, session: Session) -> str:
        """Fetch an x-csrf-token from $metadata (required before mutating requests)."""
        url = f"{self.url}/$metadata"
        self._btp_refresh(session)
        try:
            r = session.get(url, headers={"x-csrf-token": "Fetch"}, timeout=HTTP_TIMEOUT)
            token = r.headers.get("x-csrf-token", "")
            sys.stderr.write(
                f"[bridge] {self.alias}: CSRF token fetch "
                f"{'ok' if token else 'empty (no token returned)'}\n"
            )
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
        self._btp_refresh(self._bootstrap_session)
        try:
            r = self._bootstrap_session.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            raw = r.content
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
                pdata: dict = {
                    "type":       edm_to_json(ptype),
                    "edm_type":   ptype,
                    "nullable":   nullable,
                    "label":      label,
                    "internal":   _is_internal_prop(pname, ptype),
                }
                # v2 property-level sap:creatable / sap:updatable annotations
                if not is_v4:
                    sap_p_crt = prop.get(f"{{{SAP_NS}}}creatable", "") or prop.get("sap:creatable", "")
                    sap_p_upd = prop.get(f"{{{SAP_NS}}}updatable", "") or prop.get("sap:updatable", "")
                    if sap_p_crt == "false":
                        pdata["sap_creatable"] = False
                    if sap_p_upd == "false":
                        pdata["sap_updatable"] = False
                props[pname] = pdata
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

        # ---- Post-process: parse external <Annotations Target="..."> blocks ----
        # SAP always places capability annotations in external <Annotations> blocks
        # using the OData v4 namespace — even in v2 hybrid metadata.  Run for all
        # service versions.
        self._apply_external_annotations(schema, ns, schema_is_v4=is_v4)

        sys.stderr.write(
            f"[bridge] {self.alias}: "
            f"OData v{self.odata_version}, "
            f"{len(self.entity_sets)} entity sets, "
            f"{len(self.actions)} actions/functions\n"
        )

    # ------------------------------------------------------------------ #
    # External annotation processing (OData v4)                           #
    # ------------------------------------------------------------------ #

    def _apply_external_annotations(self, schema, ns: dict, schema_is_v4: bool = True) -> None:
        """
        Parse <Annotations Target="..."> blocks in the schema to:
          1. Pick up capability restrictions on entity sets that SAP places
             outside the <EntitySet> element (common in both v4 and v2-hybrid).
          2. Detect SAP__core.Computed / Org.OData.Core.V1.Computed key props
             so we can suppress create/get-by-key tools for server-generated keys.

        SAP always uses the OData v4 namespace for external <Annotations> blocks,
        even in v2 (ADO-namespace) metadata — so we search with the v4 ns here.
        """
        # External annotations always use the OData v4 namespace.
        edm_ns = "http://docs.oasis-open.org/odata/ns/edm"

        # --- Step 1: collect computed property names per entity type ---
        # Target looks like "Alias.EntityTypeName/PropertyName"
        computed_by_type: dict[str, set] = {}
        for ann_block in schema.findall(f"{{{edm_ns}}}Annotations"):
            target = ann_block.get("Target", "")
            if "/" not in target:
                continue
            type_part, prop_name = target.split("/", 1)
            type_local = type_part.split(".")[-1]
            for ann in ann_block.findall(f"{{{edm_ns}}}Annotation"):
                term = ann.get("Term", "")
                if term.endswith(".Computed") or term == "Org.OData.Core.V1.Computed":
                    if type_local not in computed_by_type:
                        computed_by_type[type_local] = set()
                    computed_by_type[type_local].add(prop_name)

        # Apply computed flag to entity set props (shallow-copy shares inner dicts)
        for es_name, es_data in self.entity_sets.items():
            props = es_data["props"]
            for type_local, computed_props in computed_by_type.items():
                for pname in computed_props:
                    if pname in props:
                        props[pname]["computed"] = True

        # --- Step 2: parse entity-set-level capability <Annotations> blocks ---
        # Target looks like "Alias.ContainerName/EntitySetName"
        for ann_block in schema.findall(f"{{{edm_ns}}}Annotations"):
            target = ann_block.get("Target", "")
            if "/" not in target:
                continue
            _, es_name = target.split("/", 1)
            if es_name not in self.entity_sets:
                continue
            caps = self.entity_sets[es_name]["capabilities"]
            for ann in ann_block.findall(f"{{{edm_ns}}}Annotation"):
                term = ann.get("Term", "")
                rec  = ann.find(f"{{{edm_ns}}}Record")
                if rec is None:
                    continue
                if term.endswith("SearchRestrictions"):
                    for pv in rec.findall(f"{{{edm_ns}}}PropertyValue"):
                        if pv.get("Property") == "Searchable":
                            caps["searchable"] = pv.get("Bool", "false").lower() == "true"
                elif term.endswith("InsertRestrictions"):
                    for pv in rec.findall(f"{{{edm_ns}}}PropertyValue"):
                        if pv.get("Property") == "Insertable":
                            b = pv.get("Bool", "")
                            if b:  # only override for static bool, not Path
                                caps["creatable"] = b.lower() != "false"
                elif term.endswith("UpdateRestrictions"):
                    for pv in rec.findall(f"{{{edm_ns}}}PropertyValue"):
                        if pv.get("Property") == "Updatable":
                            b = pv.get("Bool", "")
                            if b:
                                caps["updatable"] = b.lower() != "false"
                elif term.endswith("DeleteRestrictions"):
                    for pv in rec.findall(f"{{{edm_ns}}}PropertyValue"):
                        if pv.get("Property") == "Deletable":
                            b = pv.get("Bool", "")
                            if b:
                                caps["deletable"] = b.lower() != "false"

        # --- Step 3: if ALL key properties are Computed → server-assigned, so create is
        # not meaningful (no input accepted for keys). GET by key still works fine once
        # you have the key value from a filter result — only suppress create.
        if schema_is_v4:
            for es_name, es_data in self.entity_sets.items():
                keys  = es_data["keys"]
                props = es_data["props"]
                if keys and all(props.get(k, {}).get("computed", False) for k in keys):
                    es_data["capabilities"]["creatable"] = False

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
        session:       Session = None,
        params:        dict = None,
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

        sess = session or self._session_for(auth_header)
        self._btp_refresh(sess)
        try:
            resp = sess.request(method, url, data=data, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            # Early bail-out: reject oversized responses before buffering the body
            cl = int(resp.headers.get("Content-Length", 0) or 0)
            if self.max_response_size > 0 and cl > self.max_response_size:
                sys.stderr.write(
                    f"[bridge] {self.alias}: response rejected via Content-Length "
                    f"({cl:,} B > {self.max_response_size:,} B limit)\n"
                )
                return {
                    "error": "RESPONSE_TOO_LARGE",
                    "message": (
                        f"OData response ({cl:,} B) exceeds "
                        f"--max-response-size ({self.max_response_size:,} B). "
                        "Narrow your query with $top, $select, or $filter."
                    ),
                }
            raw = resp.content
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
        except requests.HTTPError as exc:
            resp = exc.response
            body_text = ""
            try:
                body_text = resp.text if resp is not None else ""
            except Exception:
                pass
            status = resp.status_code if resp is not None else 0
            reason = resp.reason if resp is not None else str(exc)
            sys.stderr.write(f"[bridge] {self.alias}: {method} {url} → HTTP {status}\n")
            if self.verbose_errors:
                return {
                    "error":       f"HTTP {status}: {reason}",
                    "http_status": status,
                    "detail":      body_text,
                }
            return {"error": f"HTTP {status}: {reason}"}
        except Exception as exc:
            sys.stderr.write(f"[bridge] {self.alias}: {method} error: {exc}\n")
            if self.verbose_errors:
                return {"error": str(exc)}
            return {"error": "Internal error processing OData request"}

    def _get(self, url: str, params: dict = None, auth: str = "") -> dict:
        """GET + automatic v2 response normalisation."""
        result = self._request("GET", url, params=params, auth_header=auth)
        if isinstance(result, dict) and "d" in result:
            result = self._normalize_v2_response(result)
        return result

    def _mutate(self, method: str, url: str, body: dict = None, auth: str = "") -> dict:
        """Mutating request — fetches CSRF token, uses caller's session for principal propagation."""
        sess = self._session_for(auth)
        csrf = self._fetch_csrf(sess)
        return self._request(
            method, url, body=body,
            extra_headers={"x-csrf-token": csrf},
            session=sess,
        )

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

        url = f"{self.url}/{entity_set}"
        result = self._get(url, params=params, auth=auth)

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
            return self._get(f"{self.url}/{entity_set}", params=params, auth=auth)
        return self._request(
            "GET", f"{self.url}/{entity_set}/$count",
            params={"$filter": filter_expr} if filter_expr else None,
            auth_header=auth,
        )

    def search(self, entity_set: str, args: dict, auth: str = "") -> dict:
        """Full-text search on an entity set using $search."""
        search_term = args.get("$search") or args.get("search", "")
        if not search_term:
            return {"error": "Missing required parameter: search"}

        params: dict = dict(self._v2_params())
        params["$search"] = search_term
        if args.get("$select") or args.get("select"):
            params["$select"] = args.get("$select") or args.get("select")
        _stop = args.get("$top") or args.get("top")
        if _stop is None and self.default_top:
            _stop = self.default_top
        if self.max_top and _stop is not None:
            _stop = min(int(_stop), self.max_top)
        if _stop is not None:
            params["$top"] = str(_stop)

        return self._get(f"{self.url}/{entity_set}", params=params, auth=auth)

    def get(self, entity_set: str, key: str, args: dict, auth: str = "") -> dict:
        # Single-entity requests must not include $inlinecount (SAP rejects it with 400)
        params: dict = {"$format": "json"} if self.odata_version == "2" else {}
        if args.get("$select"):
            params["$select"] = args["$select"]
        if args.get("$expand"):
            params["$expand"] = args["$expand"]
        return self._get(f"{self.url}/{entity_set}({key})", params=params or None, auth=auth)

    def create(self, entity_set: str, body: dict, auth: str = "") -> dict:
        return self._mutate("POST", f"{self.url}/{entity_set}", body=body, auth=auth)

    def update(
        self, entity_set: str, key: str, body: dict,
        method: str = "PATCH", auth: str = "",
    ) -> dict:
        return self._mutate(method, f"{self.url}/{entity_set}({key})", body=body, auth=auth)

    def delete(self, entity_set: str, key: str, auth: str = "") -> dict:
        return self._mutate("DELETE", f"{self.url}/{entity_set}({key})", auth=auth)

    def call_action(
        self, action_name: str, params: dict, auth: str = ""
    ) -> dict:
        session = self._session_for(auth)
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
                return self._request("GET", url, auth_header=auth, session=session)
            else:
                csrf = self._fetch_csrf(session)
                url = f"{self.url}/{action_name}"
                return self._request(
                    http_method, url, body=params,
                    extra_headers={"x-csrf-token": csrf} if csrf else None,
                    session=session,
                )

        csrf = self._fetch_csrf(session)
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
            session=session,
        )



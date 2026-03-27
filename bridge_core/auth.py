"""
BTP Connectivity proxy and XSUAA authentication for the OData MCP Bridge.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# BTP Connectivity proxy  (on-premise access from CF)
# ---------------------------------------------------------------------------

_BTP_PROXY_URL:    str   = ""
_BTP_PROXY_TOKEN:  str   = ""
_BTP_TOKEN_EXPIRY: float = 0.0
_BTP_TOKEN_URL:    str   = ""


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
    """Return a valid BTP proxy token, refreshing 60 s before expiry."""
    global _BTP_PROXY_TOKEN, _BTP_TOKEN_EXPIRY
    if not _BTP_TOKEN_URL:
        return _BTP_PROXY_TOKEN
    if time.time() < _BTP_TOKEN_EXPIRY - 60:
        return _BTP_PROXY_TOKEN

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
# XSUAA / BTP application authentication
# ---------------------------------------------------------------------------

_XSUAA_CREDS: dict = {}
_XSUAA_INTROSPECT_URL: str = ""
_XSUAA_TOKEN_URL: str      = ""
_XSUAA_AUTH_URL: str       = ""
_XSUAA_APPNAME: str        = ""


def _init_xsuaa() -> None:
    """Read xsuaa credentials from VCAP_SERVICES if the service is bound."""
    global _XSUAA_CREDS, _XSUAA_INTROSPECT_URL, _XSUAA_TOKEN_URL
    global _XSUAA_AUTH_URL, _XSUAA_APPNAME
    vcap_raw = os.environ.get("VCAP_SERVICES", "")
    if not vcap_raw:
        return
    try:
        vcap  = json.loads(vcap_raw)
        creds = vcap.get("xsuaa", [{}])[0].get("credentials", {})
        if not creds:
            return
        _XSUAA_CREDS         = creds
        base_url             = creds.get("url", "").rstrip("/")
        _XSUAA_TOKEN_URL     = base_url + "/oauth/token"
        idp_hint = os.environ.get("IDP_HINT", "").strip()
        _XSUAA_AUTH_URL      = (
            base_url + "/oauth/authorize?idp=" + idp_hint
            if idp_hint else
            base_url + "/oauth/authorize"
        )
        _XSUAA_INTROSPECT_URL= base_url + "/introspect"
        _XSUAA_APPNAME       = creds.get("xsappname", "")
        sys.stderr.write(
            f"[bridge] XSUAA auth enabled: {base_url} "
            f"(app: {_XSUAA_APPNAME})\n"
        )
    except Exception as exc:
        sys.stderr.write(f"[bridge] XSUAA init failed: {exc}\n")


def _xsuaa_introspect(token: str) -> dict:
    """Call XSUAA /introspect and return the token info dict."""
    if not _XSUAA_INTROSPECT_URL or not _XSUAA_CREDS:
        return {"active": False, "error": "xsuaa not configured"}
    clientid     = _XSUAA_CREDS.get("clientid", "")
    clientsecret = _XSUAA_CREDS.get("clientsecret", "")
    auth         = base64.b64encode(f"{clientid}:{clientsecret}".encode()).decode()
    data         = urllib.parse.urlencode({"token": token}).encode()
    req          = urllib.request.Request(
        _XSUAA_INTROSPECT_URL,
        data    = data,
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("active"):
                sys.stderr.write(f"[bridge] XSUAA introspect: token not active — {result}\n")
            return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"[bridge] XSUAA introspect HTTP {exc.code}: {body}\n")
        return {"active": False, "error": body}
    except Exception as exc:
        sys.stderr.write(f"[bridge] XSUAA introspect error: {exc}\n")
        return {"active": False, "error": str(exc)}


def _xsuaa_oauth_metadata(bridge_origin: str = "") -> dict:
    """Return an OAuth 2.0 Authorization Server Metadata document (RFC 8414)."""
    base = _XSUAA_CREDS.get("url", "").rstrip("/")
    return {
        "issuer":                                base,
        "authorization_endpoint":                (bridge_origin or base) + "/authorize",
        "token_endpoint":                        _XSUAA_TOKEN_URL,
        "introspection_endpoint":                _XSUAA_INTROSPECT_URL,
        "registration_endpoint":                 (bridge_origin or base) + "/register",
        "scopes_supported":                      ["openid"],
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported":      ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
    }

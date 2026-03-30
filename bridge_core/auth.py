"""
BTP Connectivity proxy and XSUAA authentication for the OData MCP Bridge.
"""

import base64
import hashlib
import json
import os
import sys
import threading
import time

import requests


# ---------------------------------------------------------------------------
# BTP Connectivity proxy  (on-premise access from CF)
# ---------------------------------------------------------------------------

_BTP_PROXY_URL:    str   = ""
_BTP_PROXY_TOKEN:  str   = ""
_BTP_TOKEN_EXPIRY: float = 0.0
_BTP_TOKEN_URL:    str   = ""
_BTP_LOCK = threading.Lock()


def _btp_fetch_token(clientid: str, clientsecret: str, token_url: str) -> None:
    """Fetch a new BTP proxy token and update the global expiry timestamp."""
    global _BTP_PROXY_TOKEN, _BTP_TOKEN_EXPIRY
    r = requests.post(
        token_url,
        data    = {"grant_type": "client_credentials"},
        auth    = (clientid, clientsecret),
        timeout = 30,
    )
    r.raise_for_status()
    payload           = r.json()
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

    with _BTP_LOCK:
        # Re-check after acquiring lock — another thread may have refreshed already
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

# Introspection cache: token_hash -> (result_dict, expires_at)
_INTROSPECT_CACHE: dict = {}
_INTROSPECT_LOCK = threading.Lock()
_INTROSPECT_CACHE_TTL = 300   # seconds — cache active tokens for up to 5 minutes
_INTROSPECT_CACHE_MAX = 1000  # max entries; oldest evicted when exceeded


def _jwt_exp(token: str) -> float:
    """Decode the JWT payload (no signature verification) and return exp as a Unix timestamp.
    Returns 0.0 if the token is malformed or has no exp claim."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        # JWT uses URL-safe base64 without padding
        padded = parts[1] + "==" * ((-len(parts[1])) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return float(payload.get("exp", 0))
    except Exception:
        return 0.0


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
    """Call XSUAA /introspect and return the token info dict.

    Results are cached for up to _INTROSPECT_CACHE_TTL seconds (keyed by
    SHA-256 of the token).  The cache entry is evicted at the earlier of:
    - the token's own JWT exp claim, or
    - now + _INTROSPECT_CACHE_TTL.
    This avoids a round-trip to XSUAA on every MCP request while still
    honouring short-lived tokens and revoking cached entries promptly.
    """
    if not _XSUAA_INTROSPECT_URL or not _XSUAA_CREDS:
        return {"active": False, "error": "xsuaa not configured"}

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()

    with _INTROSPECT_LOCK:
        cached = _INTROSPECT_CACHE.get(token_hash)
        if cached is not None:
            result, expires_at = cached
            if now < expires_at:
                return result
            del _INTROSPECT_CACHE[token_hash]

    clientid     = _XSUAA_CREDS.get("clientid", "")
    clientsecret = _XSUAA_CREDS.get("clientsecret", "")
    try:
        r = requests.post(
            _XSUAA_INTROSPECT_URL,
            data    = {"token": token},
            auth    = (clientid, clientsecret),
            timeout = 10,
        )
        r.raise_for_status()
        result = r.json()
        if not result.get("active"):
            sys.stderr.write(f"[bridge] XSUAA introspect: token not active — {result}\n")
            return result
        # Cache only active tokens; expire at min(jwt exp, now + TTL)
        jwt_exp = _jwt_exp(token)
        expires_at = min(
            jwt_exp if jwt_exp > now else now + _INTROSPECT_CACHE_TTL,
            now + _INTROSPECT_CACHE_TTL,
        )
        with _INTROSPECT_LOCK:
            _INTROSPECT_CACHE[token_hash] = (result, expires_at)
            if len(_INTROSPECT_CACHE) > _INTROSPECT_CACHE_MAX:
                # Evict the entry with the earliest expiry
                oldest = min(_INTROSPECT_CACHE, key=lambda k: _INTROSPECT_CACHE[k][1])
                del _INTROSPECT_CACHE[oldest]
        return result
    except requests.HTTPError as exc:
        resp = exc.response
        body = resp.text if resp is not None else str(exc)
        status = resp.status_code if resp is not None else 0
        sys.stderr.write(f"[bridge] XSUAA introspect HTTP {status}: {body}\n")
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

"""
Utility functions for the OData MCP Bridge.

Includes: .env loader, type conversion, date conversion, operation filtering,
pattern matching, input guards, and cookie helpers.
"""

import datetime
import fnmatch
import os
import pathlib
import sys

from .constants import (
    EDM_TO_JSON,
    _LEGACY_DATE_RE,
    OP_READ,
    OP_SEARCH,
    OP_FILTER,
    OP_GET,
    OP_ACTION,
)


# ---------------------------------------------------------------------------
# .env loader
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
        val = val.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Type conversion
# ---------------------------------------------------------------------------

def edm_to_json(edm_type: str) -> str:
    return EDM_TO_JSON.get(edm_type, "string")


def expand_env(value: str) -> str:
    """Expand ${VAR} placeholders in config strings."""
    import re
    return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------------------
# SAP legacy date conversion  /Date(ms±offset)/ -> ISO-8601
# ---------------------------------------------------------------------------

def convert_legacy_dates(obj):
    """
    Recursively walk a JSON-decoded structure and replace every
    /Date(timestamp)/ string with a proper ISO-8601 UTC string.
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
# Granular operation filter  (C / S / F / G / U / D / A / R)
# ---------------------------------------------------------------------------

def _expand_op_string(ops: str) -> set:
    """Expand the shorthand R -> {S,F,G} and return a set of single-char codes."""
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
            self._allowed = None

    def allows(self, op: str) -> bool:
        if self._allowed is None:
            return True
        return op.upper() in self._allowed


# ---------------------------------------------------------------------------
# Wildcard entity matching via fnmatch
# ---------------------------------------------------------------------------

def matches_patterns(name: str, patterns: list) -> bool:
    """True if *name* matches any pattern (supports * and ? wildcards)."""
    if not patterns:
        return True
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# ---------------------------------------------------------------------------
# Input guard
# ---------------------------------------------------------------------------

_MAX_STRING_PARAM = 4096

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
# Cookie helpers
# ---------------------------------------------------------------------------

def load_cookies_from_file(path: str) -> dict:
    """
    Parse a Netscape / curl cookie file into a {name: value} dict.
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
    """Parse 'key1=val1; key2=val2' -> {key1: val1, key2: val2}."""
    import http.cookies
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(cookie_str)
        return {k: m.value for k, m in jar.items()}
    except http.cookies.CookieError:
        cookies: dict = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        return cookies

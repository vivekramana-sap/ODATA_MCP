#!/usr/bin/env python3
"""
JAM OData MCP Bridge (Enhanced v2.0)
======================================
Multi-service OData v4 → MCP bridge server.

Key features:
  - Streamable HTTP transport (MCP 2025-03-26 spec)
  - SAP legacy /Date(ms)/ → ISO-8601 conversion (default ON)
  - Granular op filtering: --enable / --disable (C/S/F/G/U/D/A/R)
  - MCP Bearer token auth: --mcp-token / --mcp-token-file
  - Wildcard entity filtering via fnmatch (Product*, Order*)
  - Cookie file (Netscape) and cookie-string authentication
  - Graceful SIGTERM / SIGINT shutdown
  - --trace: dump all tools + exit (debug mode)
  - --read-only-but-functions: hide CUD, keep actions
  - --sort-tools / --no-sort-tools
  - --max-items: hard cap on returned rows + pagination hint
  - --verbose-errors: full HTTP detail in error responses
  - BTP Connectivity proxy (VCAP_SERVICES / CF on-premise tunnel)
  - Multi-service architecture with aliases (services.json)
  - Passthrough auth (forward caller credentials to OData)
  - CORS for Copilot Studio and browser clients
  - Uses requests library for HTTP

Module structure (bridge_core/ package):
  bridge_core/
    __init__.py      — Package exports
    constants.py     — EDM mappings, operation codes, regex patterns
    helpers.py       — .env loader, type conversion, date handling, OpFilter,
                       pattern matching, input guards, cookie helpers
    auth.py          — BTP Connectivity proxy, XSUAA authentication
    odata_service.py — ODataService class (metadata, CRUD, actions)
    bridge.py        — Bridge class (tool generation, MCP dispatch)
    transports.py    — Streamable HTTP handler, ThreadingHTTPServer, trace mode
    config.py        — services.json loader
  server.py         — Entry point (this file): argparse + main()

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
import os
import signal
import sys
from collections import defaultdict

from bridge_core import (
    _load_dotenv,
    _init_btp_proxy,
    _init_xsuaa,
    ThreadingHTTPServer,
    Bridge,
    print_trace,
    make_http_handler,
    load_services,
)


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
        help="HTTP port (default: 7777, env: PORT)",
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
            with open(args.mcp_token_file) as fh:
                mcp_token = fh.read().strip()
        except OSError as exc:
            sys.stderr.write(f"[bridge] cannot read token file: {exc}\n")
            sys.exit(1)

    _init_btp_proxy()
    _init_xsuaa()

    services = load_services(args.config, args)
    if not services:
        sys.stderr.write("[bridge] no services loaded - check your config\n")
        sys.exit(1)

    bridge = Bridge(services, sort_tools=args.sort_tools, verbose=args.verbose)

    # ---- Build per-group sub-bridges for /mcp/<group> routing ----
    _groups: dict = defaultdict(list)
    for svc in services:
        if svc.group:
            _groups[svc.group].append(svc)

    bridges: dict = {"": bridge}  # "" → default /mcp endpoint (all services)
    for _gname, _gsvcs in _groups.items():
        bridges[_gname] = Bridge(_gsvcs, sort_tools=args.sort_tools, verbose=args.verbose)

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

    # ---- HTTP transport (Streamable HTTP) ----
    MCPHandler = make_http_handler(
        bridges, mcp_token=mcp_token, passthrough=args.passthrough
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

    if mcp_token and args.username:
        auth_mode = f"bearer token + basic ({args.username})"
    elif mcp_token:
        auth_mode = "bearer token"
    elif args.username:
        auth_mode = f"basic ({args.username})"
    else:
        auth_mode = "open (no auth)"
    sys.stderr.write(
        f"[bridge] listening on http://{args.host}:{args.port}/mcp\n"
        f"[bridge] auth: {auth_mode} | "
        f"tools: {len(bridge._all_tools)} | "
        f"transport: http\n"
    )
    if len(bridges) > 1:
        for _gname in sorted(k for k in bridges if k):
            _b = bridges[_gname]
            sys.stderr.write(
                f"[bridge]   /mcp/{_gname} → {len(_b._all_tools)} tools "
                f"({', '.join(_b.services.keys())})\n"
            )

    if args.verbose:
        for svc in services:
            sys.stderr.write(
                f"[bridge]   {svc.alias}: "
                f"{len(svc.entity_sets)} entity sets, "
                f"{len(svc.actions)} actions | "
                f"legacy_dates={svc.legacy_dates} "
                f"max_items={svc.max_items}\n"
            )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[bridge] stopped\n")


if __name__ == "__main__":
    main()
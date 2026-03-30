"""
bridge_core — OData MCP Bridge package.

Modules:
  constants      — EDM mappings, operation codes, regex patterns
  helpers        — .env loader, type conversion, date handling, OpFilter,
                   pattern matching, input guards, cookie helpers
  auth           — BTP Connectivity proxy, XSUAA authentication
  odata_service  — ODataService class (metadata, CRUD, actions)
  bridge         — Bridge class (tool generation, MCP dispatch)
  transports     — Streamable HTTP handler, ThreadingHTTPServer, trace mode
  config         — services.json loader
  _ui            — Embedded browser test UI (HTML constant)
"""

from .helpers import _load_dotenv
from .auth import _init_btp_proxy, _init_xsuaa
from .odata_service import ODataService
from .bridge import Bridge
from .transports import ThreadingHTTPServer, print_trace, make_http_handler
from .config import load_services

__all__ = [
    "_load_dotenv",
    "_init_btp_proxy",
    "_init_xsuaa",
    "ThreadingHTTPServer",
    "ODataService",
    "Bridge",
    "print_trace",
    "make_http_handler",
    "load_services",
]

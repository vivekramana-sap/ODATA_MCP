#!/usr/bin/env python3
"""
JAM OData MCP Bridge — Configurator
=====================================
Web UI for configuring, testing and deploying the MCP bridge.

Usage:
    python3 configurator.py [--port 7770]

Then open http://localhost:7770 in your browser.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICES_PATH = os.path.join(BASE_DIR, "services.json")
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.mtaext")
DEPLOY_SCRIPT = os.path.join(BASE_DIR, "deploy.sh")
UI_PORT = int(os.environ.get("UI_PORT", "3001"))
MCP_PORT = int(os.environ.get("MCP_PORT", "7777"))
HTTP_TIMEOUT = 20
CF_APP_NAME = "jam-odata-mcp-bridge-v2"

_bridge_proc = None
_bridge_lock = threading.Lock()
_bridge_log: list = []
_bridge_log_lock = threading.Lock()
_MAX_LOG = 300

# OData EDM namespaces (v4 and v2)
EDM_NAMESPACES = [
    "http://docs.oasis-open.org/odata/ns/edm",
    "http://schemas.microsoft.com/ado/2008/09/edm",
    "http://schemas.microsoft.com/ado/2007/05/edm",
    "http://schemas.microsoft.com/ado/2006/04/edm",
]


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def expand_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def load_env_from_creds():
    """Load credentials from credentials.mtaext into env vars (always overwrites)."""
    creds = read_credentials()
    for key, val in creds.items():
        if val:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Bridge process management
# ---------------------------------------------------------------------------

def _bridge_status() -> dict:
    global _bridge_proc
    if _bridge_proc is None or _bridge_proc.poll() is not None:
        _bridge_proc = None
        return {"running": False, "pid": None}
    return {"running": True, "pid": _bridge_proc.pid}


def _bridge_endpoints() -> dict:
    """Return a mapping of group → MCP endpoint URL derived from services.json."""
    base = f"http://localhost:{MCP_PORT}"
    endpoints = {"default": f"{base}/mcp"}
    try:
        services = read_services()
        groups: dict = {}
        for svc in services:
            g = svc.get("group", "").strip()
            if g and g not in groups:
                groups[g] = []
            if g:
                groups[g].append(svc.get("alias", ""))
        for gname, aliases in groups.items():
            endpoints[gname] = f"{base}/mcp/{gname}"
    except Exception:
        pass
    return {"endpoints": endpoints}


def _bridge_start() -> dict:
    global _bridge_proc, _bridge_log
    with _bridge_lock:
        if _bridge_proc and _bridge_proc.poll() is None:
            return {"ok": False, "error": "Bridge already running", "pid": _bridge_proc.pid}
        try:
            creds = read_credentials()
            cmd = [sys.executable, os.path.join(BASE_DIR, "server.py"),
                   "--config", SERVICES_PATH, "--port", str(MCP_PORT)]
            if creds.get("MCP_TOKEN"):
                cmd += ["--mcp-token", creds["MCP_TOKEN"]]
            if creds.get("MCP_USERNAME"):
                cmd += ["--username", creds["MCP_USERNAME"]]
            if creds.get("MCP_PASSWORD"):
                cmd += ["--password", creds["MCP_PASSWORD"]]
            proc = subprocess.Popen(
                cmd,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _bridge_proc = proc
            with _bridge_log_lock:
                _bridge_log = []

            def _reader(p):
                for line in p.stdout:
                    sys.stderr.write(f"[bridge-out] {line}")
                    with _bridge_log_lock:
                        _bridge_log.append(line.rstrip())
                        if len(_bridge_log) > _MAX_LOG:
                            _bridge_log.pop(0)

            threading.Thread(target=_reader, args=(proc,), daemon=True).start()
            return {"ok": True, "pid": proc.pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _bridge_stop() -> dict:
    global _bridge_proc
    with _bridge_lock:
        if _bridge_proc is None or _bridge_proc.poll() is not None:
            _bridge_proc = None
            return {"ok": True, "message": "Not running"}
        try:
            _bridge_proc.terminate()
            try:
                _bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bridge_proc.kill()
            _bridge_proc = None
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Services config
# ---------------------------------------------------------------------------

def read_services() -> list:
    try:
        with open(SERVICES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def write_services(data: list):
    tmp = SERVICES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SERVICES_PATH)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def read_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH) as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    result = {}
    for key in ("MCP_USERNAME", "MCP_PASSWORD", "MCP_TOKEN"):
        m = re.search(rf'^\s+{key}:\s+"?(.*?)"?\s*$', text, re.MULTILINE)
        result[key] = m.group(1) if m else ""
    return result


def write_credentials(updates: dict):
    # Preserve existing keys; only update what's sent
    existing = read_credentials()
    existing.update({k: v for k, v in updates.items() if v != ""})

    props = "\n".join(f'      {k}: "{v}"' for k, v in existing.items() if v)
    content = f"""_schema-version: "3.2"
ID: jam-odata-mcp-bridge-v2-credentials
extends: jam-odata-mcp-bridge-v2

modules:
  - name: jam-odata-mcp-bridge-v2
    properties:
{props}
"""
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, CREDENTIALS_PATH)


# ---------------------------------------------------------------------------
# Probe OData metadata
# ---------------------------------------------------------------------------

def _detect_edm_ns(root) -> str:
    """Detect the EDM namespace from the parsed XML root."""
    for ns in EDM_NAMESPACES:
        if root.find(f".//{{{ns}}}Schema") is not None:
            return ns
    # Fallback: scan all tags for any Schema element
    for elem in root.iter():
        tag = elem.tag
        if tag.endswith("}Schema"):
            return tag[1:tag.index("}")]
    return EDM_NAMESPACES[0]


def probe_service(item: dict) -> dict:
    """Fetch $metadata for an OData service and return entity set info."""
    url = expand_env(item.get("url", "")).rstrip("/")
    username = expand_env(item.get("username", ""))
    password = expand_env(item.get("password", ""))

    meta_url = f"{url}/$metadata"
    req = urllib.request.Request(meta_url, headers={"Accept": "application/xml"})

    if username:
        import base64
        av = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_unredirected_header("Authorization", av)

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            root = ET.fromstring(r.read())
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} {e.reason}"
        if e.code == 401:
            msg += " — check username/password in the service config (or use passthrough auth)"
        elif e.code == 403:
            msg += " — user lacks authorization to read $metadata"
        elif e.code == 404:
            msg += " — URL not found; verify the service path"
        return {"success": False, "error": msg, "hint": "http"}
    except urllib.error.URLError as e:
        import urllib.parse as _up
        if isinstance(e.reason, socket.gaierror):
            host = _up.urlparse(url).hostname or url
            return {"success": False,
                    "error": f"Cannot resolve hostname '{host}'. "
                             f"Make sure you are connected to VPN or the host is reachable from this machine.",
                    "hint": "dns"}
        reason = str(e.reason)
        if "timed out" in reason or "timeout" in reason.lower():
            return {"success": False,
                    "error": f"Connection timed out after {HTTP_TIMEOUT}s. "
                             f"The host may be unreachable — check VPN or firewall.",
                    "hint": "timeout"}
        if "Connection refused" in reason:
            host = _up.urlparse(url).netloc or url
            return {"success": False,
                    "error": f"Connection refused by {host}. Verify the host and port.",
                    "hint": "refused"}
        return {"success": False, "error": reason}
    except ET.ParseError as e:
        return {"success": False,
                "error": f"Could not parse $metadata XML: {e}. "
                          f"The endpoint may not be an OData service.",
                "hint": "parse"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    ns = _detect_edm_ns(root)

    # Collect entity types
    entity_types = {}
    for et in root.iter(f"{{{ns}}}EntityType"):
        name = et.get("Name", "")
        keys = [p.get("Name") for p in et.findall(
            f"{{{ns}}}Key/{{{ns}}}PropertyRef")]
        props = [p.get("Name", "") for p in et.findall(f"{{{ns}}}Property")]
        entity_types[name] = {"keys": keys, "props": props}

    # Collect entity sets
    entity_sets = []
    for container in root.iter(f"{{{ns}}}EntityContainer"):
        for es in container.findall(f"{{{ns}}}EntitySet"):
            es_name = es.get("Name", "")
            et_name = es.get("EntityType", "").split(".")[-1]
            et_data = entity_types.get(et_name, {"keys": [], "props": []})
            entity_sets.append({
                "name": es_name,
                "keys": et_data["keys"],
                "prop_count": len(et_data["props"]),
                "fields": et_data["props"],
            })

    # Collect actions
    actions = [a.get("Name", "") for a in root.iter(f"{{{ns}}}Action")]

    return {
        "success": True,
        "entity_sets": entity_sets,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# CF helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]')

def _cf(*args, timeout: int = 20) -> dict:
    """Run a CF CLI command and return {ok, stdout, stderr, output, returncode}."""
    cmd = ["cf"] + list(args)
    print(f"[cf] Running: {' '.join(cmd)}", flush=True)
    try:
        out = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,  # non-interactive: avoids hanging on org/space prompts
        )
        stdout = _ANSI_RE.sub('', out.stdout).strip()
        stderr = _ANSI_RE.sub('', out.stderr).strip()
        output = (stdout + ("\n" + stderr if stderr else "")).strip()
        if not output:
            output = f"cf {args[0]} exited with code {out.returncode} — no output (stdout and stderr both empty)"
        print(f"[cf] rc={out.returncode}  output_len={len(output)}\n{output[:300]}", flush=True)
        return {
            "ok": out.returncode == 0,
            "output": output,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": out.returncode,
        }
    except FileNotFoundError:
        msg = "cf CLI not found in PATH"
        print(f"[cf] ERROR: {msg}", flush=True)
        return {"ok": False, "output": msg, "stdout": "", "stderr": "", "returncode": -1}
    except subprocess.TimeoutExpired:
        msg = f"cf command timed out after {timeout}s — login may still be in progress"
        print(f"[cf] ERROR: {msg}", flush=True)
        return {"ok": False, "output": msg, "stdout": "", "stderr": "", "returncode": -1}
    except Exception as e:
        msg = str(e)
        print(f"[cf] ERROR: {msg}", flush=True)
        return {"ok": False, "output": msg, "stdout": "", "stderr": "", "returncode": -1}


def cf_login(api: str, username: str, password: str,
             org: str = "", space: str = "") -> dict:
    args = ["login", "-a", api, "-u", username, "-p", password]
    if org:   args += ["-o", org]
    if space: args += ["-s", space]
    args.append("--skip-ssl-validation")
    return _cf(*args, timeout=60)


def cf_logout() -> dict:
    return _cf("logout")


def cf_target() -> dict:
    return _cf("target")


def cf_app_status() -> dict:
    """Return running status of the deployed app."""
    r = _cf("app", CF_APP_NAME, timeout=20)
    if not r["ok"] and "not found" in r["output"].lower():
        return {"ok": False, "deployed": False, "output": r["output"]}
    if not r["ok"]:
        return {"ok": False, "deployed": None, "output": r["output"]}
    # Parse key fields from cf app output
    out = r["output"]
    state  = _parse_cf_field(out, "requested state")
    memory = _parse_cf_field(out, "memory usage")
    routes = _parse_cf_field(out, "routes")
    instances = _parse_cf_field(out, "instances")
    return {
        "ok": True,
        "deployed": True,
        "state":     state,
        "routes":    routes,
        "memory":    memory,
        "instances": instances,
        "output":    out,
    }


def _parse_cf_field(text: str, label: str) -> str:
    m = re.search(rf'^{re.escape(label)}:\s*(.+)$', text, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def cf_checklist() -> dict:
    # 1. CF CLI installed
    cli_result = _cf("version")
    cli_ok     = cli_result["ok"]
    cli_ver    = cli_result["output"].split("\n")[0] if cli_ok else ""

    # 2. Logged in
    target     = cf_target()
    logged_in  = target["ok"]
    target_out = target["output"]

    # 3. credentials.mtaext exists and is non-empty
    creds_ok = os.path.isfile(CREDENTIALS_PATH) and os.path.getsize(CREDENTIALS_PATH) > 0

    # 4. services.json has at least one entry
    svcs     = read_services()
    svc_ok   = len(svcs) > 0

    # 5. App already deployed
    app      = cf_app_status()
    app_state = app.get("state", "").lower()
    app_ok   = app.get("deployed", False) and app_state == "started"

    return {
        "cli":        {"ok": cli_ok,   "detail": cli_ver},
        "logged_in":  {"ok": logged_in, "detail": target_out},
        "creds":      {"ok": creds_ok,  "detail": CREDENTIALS_PATH if creds_ok else "File missing or empty"},
        "services":   {"ok": svc_ok,   "detail": f"{len(svcs)} service(s) configured"},
        "app":        {"ok": app_ok,   "detail": app.get("routes", "") or app.get("output", ""),
                       "deployed": app.get("deployed"),
                       "state": app.get("state", ""),
                       "routes": app.get("routes", "")},
    }


# ---------------------------------------------------------------------------
# MCP bridge proxy (for tool testing)
# ---------------------------------------------------------------------------

def _mcp_call(method: str, params: dict = None, auth: str = "") -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": method,
        "params": params or {},
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(
        f"http://localhost:{MCP_PORT}/mcp",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        return {"error": f"MCP server not reachable on port {MCP_PORT}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class ConfiguratorHandler(BaseHTTPRequestHandler):

    # Paths whose repeated polls should not flood the terminal
    _SILENT_PATHS = {"/api/bridge/status", "/api/bridge/logs"}

    def log_message(self, fmt, *args):
        # Suppress noisy poll endpoints
        msg = fmt % args
        for p in self._SILENT_PATHS:
            if p in msg:
                return
        sys.stderr.write(f"[configurator] {msg}\n")

    # -- Helpers ----------------------------------------------------------------

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allowed = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
        if allowed:
            allowed_set = {o.strip() for o in allowed.split(",") if o.strip()}
            if origin in allowed_set:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _check_auth(self) -> bool:
        """Validate configurator access.

        If CONFIGURATOR_TOKEN is set, require it as a Bearer token.
        If not set, allow local requests only (127.0.0.1 / ::1).
        """
        token = os.environ.get("CONFIGURATOR_TOKEN", "").strip()
        if token:
            ah = self.headers.get("Authorization", "")
            return ah == f"Bearer {token}"
        # No token configured: restrict to localhost
        client_ip = self.client_address[0]
        return client_ip in ("127.0.0.1", "::1")

    def _json(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _serve_file(self, path: str, content_type: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._json(404, {"error": f"File not found: {path}"})

    # -- Routing ----------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html", "/index_react.html"):
            host = self.headers.get("Host", "")
            import re
            bas_match = re.match(r"^port\d+(-[\w\-.]+)$", host)
            if bas_match:
                ui_url = f"https://port{UI_PORT}{bas_match.group(1)}"
            else:
                ui_url = f"http://localhost:{UI_PORT}"
            self.send_response(302)
            self._cors()
            self.send_header("Location", ui_url)
            self.end_headers()
            return

        # All /api/* endpoints require auth
        if path.startswith("/api/") and not self._check_auth():
            self._json(401, {"error": "Unauthorized"})
            return

        if path == "/api/services":
            self._json(200, read_services())

        elif path == "/api/credentials":
            self._json(200, read_credentials())

        elif path == "/api/tools":
            result = _mcp_call("tools/list")
            if "result" in result:
                self._json(200, {"tools": result["result"]["tools"]})
            else:
                self._json(200, {"tools": [], "error": result.get("error", "unknown")})

        elif path == "/api/cf-status":
            r = cf_target()
            self._json(200, {"ok": r["ok"], "output": r["output"]})

        elif path == "/api/cf/checklist":
            self._json(200, cf_checklist())

        elif path == "/api/cf/app":
            self._json(200, cf_app_status())

        elif path == "/api/btp/health":
            app = cf_app_status()
            routes = app.get("routes", "").strip()
            if not routes:
                self._json(200, {"ok": False, "error": "App not deployed or no routes found"})
                return
            url = f"https://{routes}/health"
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                self._json(200, {"ok": True, "url": url, **data})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e), "url": url})

        elif path == "/api/btp/endpoints":
            app = cf_app_status()
            routes = app.get("routes", "").strip()
            if not routes:
                self._json(200, {"ok": False, "error": "App not deployed or no routes found"})
                return
            url = f"https://{routes}/mcp"
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                self._json(200, {"ok": True, "url": url, **data})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e), "url": url})

        elif path == "/api/bridge/status":
            self._json(200, _bridge_status())

        elif path == "/api/bridge/endpoints":
            self._json(200, _bridge_endpoints())

        elif path == "/api/bridge/logs":
            with _bridge_log_lock:
                logs = list(_bridge_log)
            running = _bridge_status()["running"]
            self._json(200, {"logs": logs, "running": running})

        elif path == "/api/deploy":
            self._stream_deploy()

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path

        if path.startswith("/api/") and not self._check_auth():
            self._json(401, {"error": "Unauthorized"})
            return

        if path == "/api/probe":
            data = self._body()
            self._json(200, probe_service(data))

        elif path == "/api/tools/call":
            data = self._body()
            auth = data.get("auth", "") or self.headers.get("Authorization", "")
            result = _mcp_call(
                "tools/call",
                {"name": data.get("name", ""), "arguments": data.get("arguments", {})},
                auth=auth,
            )
            self._json(200, result)

        elif path == "/api/bridge/start":
            self._json(200, _bridge_start())

        elif path == "/api/bridge/stop":
            self._json(200, _bridge_stop())

        elif path == "/api/cf/login":
            data = self._body()
            result = cf_login(
                data.get("api", ""),
                data.get("username", ""),
                data.get("password", ""),
                data.get("org", ""),
                data.get("space", ""),
            )
            self._json(200, result)

        elif path == "/api/cf/logout":
            self._json(200, cf_logout())

        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):
        path = self.path

        if path.startswith("/api/") and not self._check_auth():
            self._json(401, {"error": "Unauthorized"})
            return

        if path == "/api/services":
            data = self._body()
            if not isinstance(data, list):
                self._json(400, {"error": "Expected a JSON array"})
                return
            write_services(data)
            self._json(200, {"ok": True})

        elif path == "/api/credentials":
            data = self._body()
            write_credentials(data)
            # Reload env vars
            load_env_from_creds()
            self._json(200, {"ok": True})

        else:
            self._json(404, {"error": "not found"})

    # -- SSE Deploy Stream ------------------------------------------------------

    def _stream_deploy(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        def send(data: dict):
            msg = f"data: {json.dumps(data)}\n\n"
            self.wfile.write(msg.encode())
            self.wfile.flush()

        try:
            proc = subprocess.Popen(
                ["bash", DEPLOY_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=BASE_DIR,
            )
            for line in proc.stdout:
                send({"line": line.rstrip()})
            proc.wait()
            send({"exit": proc.returncode})
        except FileNotFoundError:
            send({"line": f"ERROR: deploy.sh not found at {DEPLOY_SCRIPT}"})
            send({"exit": 1})
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                send({"line": f"ERROR: {e}"})
                send({"exit": 1})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="JAM OData MCP Bridge Configurator")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CONFIGURATOR_PORT", 7770)))
    args = parser.parse_args()

    os.makedirs(os.path.join(BASE_DIR, "ui"), exist_ok=True)
    load_env_from_creds()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ConfiguratorHandler)
    sys.stderr.write(f"[configurator] http://localhost:{args.port}\n")
    sys.stderr.write(f"[configurator] MCP bridge expected on port {MCP_PORT}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[configurator] stopped\n")


if __name__ == "__main__":
    main()

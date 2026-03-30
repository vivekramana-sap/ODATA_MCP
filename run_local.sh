#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_local.sh  —  Start the OData MCP Bridge locally for testing
#
# Starts the same bridge that runs on BTP, but on localhost.
# Endpoints mirror BTP exactly:
#
#   BTP:    https://jam-odata-mcp-bridge-v2.cfapps.eu10.hana.ondemand.com/mcp/bp
#   Local:  http://localhost:7777/mcp/bp
#
#   BTP:    .../mcp/ean
#   Local:  http://localhost:7777/mcp/ean
#
#   BTP:    .../mcp/products
#   Local:  http://localhost:7777/mcp/products
#
# Usage:
#   ./run_local.sh             # starts on port 7777 (default)
#   ./run_local.sh --port 8080 # custom port
#   ./run_local.sh --trace     # dump all tools and exit (no server)
#
# MCP client config (Claude Desktop / MCP inspector / curl):
#   "url": "http://localhost:7777/mcp/bp"
#
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PORT=${PORT:-7777}
CONFIG=${CONFIG:-services.json}

# Allow overriding port via --port flag
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *)      break ;;
  esac
done

echo "========================================"
echo "  OData MCP Bridge — Local Dev Server"
echo "========================================"
echo ""
echo "  Config : $CONFIG"
echo "  Port   : $PORT"
echo ""
echo "  Endpoints (same groups as BTP):"
echo "    http://localhost:$PORT/mcp         ← all services"
echo "    http://localhost:$PORT/mcp/bp      ← Business Partner"
echo "    http://localhost:$PORT/mcp/ean     ← EAN services"
echo "    http://localhost:$PORT/mcp/products← Products"
echo ""
echo "  Health : http://localhost:$PORT/health"
echo "  Tools  : curl -s http://localhost:$PORT/mcp/bp"
echo ""
echo "  MCP client (Claude Desktop / Inspector):"
echo "    \"url\": \"http://localhost:$PORT/mcp/bp\""
echo ""
echo "Press Ctrl+C to stop."
echo "----------------------------------------"

exec python3 server.py \
  --config "$CONFIG" \
  --port "$PORT" \
  --transport http \
  --verbose \
  "$@"

#!/usr/bin/env bash
# JAM OData MCP Bridge — BTP CF Deployment via MTA
# Usage:
#   1. Copy credentials.mtaext.template -> credentials.mtaext and fill in values
#   2. Run: bash deploy.sh

set -e

MTAEXT=credentials.mtaext

echo "=== JAM OData MCP Bridge — MTA Deployment ==="
echo ""

# -- Verify CF login -----------------------------------------------------------
echo "[ 1/3 ] Checking CF login..."
cf target

# -- Check credentials.mtaext --------------------------------------------------
if [ ! -f "$MTAEXT" ]; then
  echo ""
  echo "ERROR: $MTAEXT not found."
  echo "       Copy credentials.mtaext.template to credentials.mtaext and fill in your SAP credentials."
  exit 1
fi

# -- Build MTA archive ---------------------------------------------------------
echo ""
echo "[ 2/3 ] Building MTA archive..."
mbt build -t .

MTAR=$(ls -1t jam-odata-mcp-bridge-v2_*.mtar 2>/dev/null | head -1)
if [ -z "$MTAR" ]; then
  echo "ERROR: No .mtar file found after build."
  exit 1
fi
echo "        Archive: $MTAR"

# -- Deploy to CF --------------------------------------------------------------
echo ""
echo "[ 3/3 ] Deploying $MTAR with credentials extension..."
cf deploy "$MTAR" -e "$MTAEXT" -f

# -- Done ----------------------------------------------------------------------
echo ""
echo "=== Done ==="
APP_URL=$(cf app jam-odata-mcp-bridge-v2 | grep -E "^routes:" | awk '{print $2}')
echo "    MCP endpoint: https://$APP_URL/mcp"
echo "    Health:       https://$APP_URL/health"

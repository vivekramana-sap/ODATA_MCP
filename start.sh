#!/bin/bash
BACKEND_PORT=${1:-7770}
UI_PORT=${2:-3001}

kill_port() {
  ss -tlnp "sport = :$1" 2>/dev/null | awk -F'pid=' '/pid=/{print $2}' | cut -d, -f1 | xargs -r kill -9 2>/dev/null
}

echo "Clearing ports $BACKEND_PORT and $UI_PORT..."
kill_port $BACKEND_PORT
kill_port $UI_PORT
sleep 1

echo "Starting configurator on port $BACKEND_PORT..."
python3 configurator.py --port $BACKEND_PORT &
BACKEND_PID=$!
sleep 1

if ! lsof -i:$BACKEND_PORT > /dev/null 2>&1; then
  echo "ERROR: configurator failed to start"
  exit 1
fi
echo "Backend running (PID $BACKEND_PID)"

echo "Starting UI on port $UI_PORT..."
cd ui && PORT=$UI_PORT npm run dev

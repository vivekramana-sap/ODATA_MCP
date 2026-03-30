# ── Stage 1: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="your-team"
LABEL version="3.0.0"
LABEL description="JAM OData MCP Bridge — Production Edition"

# Non-root user for security
RUN addgroup --system bridge && adduser --system --ingroup bridge bridge

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY server.py        ./
COPY services.json    ./

# Optional: copy .env if you prefer file-based secrets over env vars
# COPY .env           ./

# Health check (polls /health every 30s)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-7777}/health', timeout=4)"

USER bridge

EXPOSE 7777

# Default: HTTP transport on localhost (override with env vars)
ENV PORT=7777 \
    LOG_FILE=/tmp/bridge.log \
    MAX_RESPONSE_SIZE=5242880

ENTRYPOINT ["python3", "server.py"]
CMD ["--config", "services.json",
     "--host",      "0.0.0.0",
     "--port",      "7777",
     "--i-am-security-expert",
     "--max-response-size", "5242880",
     "--log-file",  "/tmp/bridge.log",
     "--service-info-tool"]

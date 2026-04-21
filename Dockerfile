FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .[all]

ENV BRAIN_DB=/data/brain.db
VOLUME /data

# Default entry point is the stdio MCP server — unchanged for
# Claude Desktop / local subprocess use.
CMD ["brainctl-mcp"]

# ---------------------------------------------------------------------------
# HTTP transport — sibling entry point, opt-in. Run this image with
# `--entrypoint brainctl-mcp-http` (or override `CMD`) and publish port
# 8080 to expose the Streamable HTTP surface for remote MCP clients
# (xAI Grok remote-MCP, Strand, etc.). See docs/MCP_HTTP.md for config +
# auth + allowlist details.
#
# Required env when using the HTTP entry point:
#   BRAINCTL_HTTP_TOKEN         static bearer token (>=32 chars)
#   BRAINCTL_HTTP_ALLOWED_TOOLS comma-separated MCP tool allowlist
# Optional: BRAINCTL_HTTP_{HOST,PORT,LOG_LEVEL}.
EXPOSE 8080

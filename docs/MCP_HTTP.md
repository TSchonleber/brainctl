# brainctl-mcp-http — HTTP transport for the MCP server

brainctl ships two MCP transports:

| Transport | Entry point        | Use case |
|-----------|--------------------|----------|
| stdio     | `brainctl-mcp`     | Claude Desktop, local dev, subprocess-spawned agents. Unchanged. |
| HTTP      | `brainctl-mcp-http` | Remote agents that require Streamable HTTP (xAI Grok remote-MCP, Strand, anything over a network boundary). |

The HTTP transport exposes the **same** `app: Server` instance the stdio
path uses — tools and their handlers are registered once, in
`src/agentmemory/mcp_server.py`. The HTTP module adds transport + auth +
allowlist only; no tool behaviour is duplicated.

## Install

```bash
pip install 'brainctl[mcp]'
```

The `mcp` extra now pulls Starlette, uvicorn (with standard extras),
and python-json-logger in addition to the MCP SDK itself.

## Environment

| Variable                      | Required | Default   | Notes |
|-------------------------------|----------|-----------|-------|
| `BRAINCTL_HTTP_TOKEN`         | yes      | —         | Static bearer token. Must be ≥32 chars. Boot fails loudly otherwise. |
| `BRAINCTL_HTTP_ALLOWED_TOOLS` | yes      | —         | Comma-separated list of MCP tool names exposed over HTTP. `tools/list` is filtered to this set; `tools/call` on any other name returns JSON-RPC `-32601`. |
| `BRAINCTL_HTTP_PORT`          | no       | `8080`    | TCP port. |
| `BRAINCTL_HTTP_HOST`          | no       | `0.0.0.0` | Bind address. |
| `BRAINCTL_HTTP_LOG_LEVEL`     | no       | `info`    | `debug` / `info` / `warning` / `error` / `critical`. |

Bad config → `exit 1` with a one-line stderr message before logging is
configured.

## Local run

Either via the console script or uvicorn directly:

```bash
export BRAINCTL_HTTP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
export BRAINCTL_HTTP_ALLOWED_TOOLS=memory_search,memory_add,entity_search
brainctl-mcp-http
# or:
# uvicorn "agentmemory.mcp_http:create_app" --factory --port 8080
```

## Probes

```bash
# Health — no auth.
curl -s http://localhost:8080/health
# → {"ok":true}

# Listing tools — filtered to the allowlist.
curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $BRAINCTL_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Calling an allowlisted tool.
curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $BRAINCTL_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"memory_search","arguments":{"query":"foo"}}}'
```

## Deploy

Any container host that speaks HTTP. Example (Fly.io):

```Dockerfile
FROM brainctl:2.4.12
ENV BRAINCTL_HTTP_ALLOWED_TOOLS=memory_search,entity_search
EXPOSE 8080
CMD ["brainctl-mcp-http"]
```

Set `BRAINCTL_HTTP_TOKEN` as a secret in the platform's secret manager
(`fly secrets set`, `gh secret set`, etc.) — never bake it into the
image.

## Security notes

* The bearer token is the **only** auth. Do not expose the service
  publicly without TLS (use a reverse proxy — Caddy, nginx, Cloudflare
  Tunnel — or let the host provide HTTPS termination).
* Tool arguments and results are **never** logged; only request id,
  method, tool name, duration, and status are written (JSON).
* Rate limit: 100 req/min per client IP, sliding window, in-memory.
  Multi-node deployments should front the service with an upstream
  limiter — the in-memory limiter doesn't coordinate across workers.
* Request body is capped at 1 MiB. Oversized requests get `413`.
* Graceful shutdown on `SIGTERM` drains in-flight requests for up to
  10s before closing the uvicorn loop.

## Stdio path is unchanged

`brainctl-mcp` still boots the original stdio server — same tool
registration, same dispatch, same code path. The HTTP transport is an
additive sibling, not a rewrite. Existing Claude Desktop users need to
do nothing.

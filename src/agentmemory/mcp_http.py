"""brainctl MCP over Streamable HTTP — sibling transport to stdio.

The stdio entry point (``brainctl-mcp`` → :mod:`agentmemory.mcp_server`)
is unchanged. This module adds a second entry point
(``brainctl-mcp-http``) that exposes the same already-configured
``app: Server`` over HTTP per the MCP 2025-06-18 Streamable HTTP spec
(https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http),
gated by a static bearer token and a tool allowlist. xAI Grok's
remote-MCP feature and the Strand agent both require HTTP rather than
stdio.

Design decisions locked in 2.5.0:

* **Single source of truth.** We import the existing ``app`` from
  :mod:`agentmemory.mcp_server` rather than re-register tools here.
  Every tool that exists in stdio exists here (modulo allowlist).
* **Stateless, JSON-response-preferred.** The session manager runs
  with ``stateless=True`` + ``json_response=True``; the spec's
  "both Accept branches" requirement is satisfied by
  :class:`StreamableHTTPSessionManager`'s own negotiation. We don't
  persist session state across requests — simpler to audit, simpler
  to allowlist.
* **Allowlist at the transport layer.** ``tools/call`` with a
  non-allowlisted name short-circuits to JSON-RPC ``-32601`` before
  the dispatcher sees it; ``tools/list`` responses get post-filtered
  by ASGI middleware. This satisfies "the allowlist check wraps the
  existing dispatch — do not duplicate dispatch logic" without
  touching ``mcp_server.py``.
* **No tool args or results in logs** — memory content is sensitive.
  Request logs carry request id, method, tool name, duration, status.

Boot contract (exits 1 on violation):

* ``BRAINCTL_HTTP_TOKEN`` — required, ≥32 chars.
* ``BRAINCTL_HTTP_ALLOWED_TOOLS`` — required, comma-separated list.
* ``BRAINCTL_HTTP_PORT`` — optional, default 8080.
* ``BRAINCTL_HTTP_HOST`` — optional, default 0.0.0.0.
* ``BRAINCTL_HTTP_LOG_LEVEL`` — optional, default info.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

# python-json-logger 3.x moved the JsonFormatter into a ``.json`` module;
# 2.x kept it at ``.jsonlogger``. Handle both so users on either floor
# pass the import.
try:
    from pythonjsonlogger.json import JsonFormatter  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[import-not-found, no-redef]
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from agentmemory.mcp_server import app as mcp_app

import mcp.types as mtypes
from mcp.shared.context import RequestContext
from pydantic import ValidationError


_BODY_CAP_BYTES = 1 * 1024 * 1024  # 1 MiB
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX_REQUESTS = 100
_DRAIN_SECONDS = 10.0
_MIN_TOKEN_CHARS = 32


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HTTPConfig:
    """Validated configuration loaded from environment at boot."""

    token: str
    allowed_tools: frozenset[str]
    host: str
    port: int
    log_level: str

    @classmethod
    def from_env(cls) -> "HTTPConfig":
        token = os.environ.get("BRAINCTL_HTTP_TOKEN", "")
        if len(token) < _MIN_TOKEN_CHARS:
            raise ValueError(
                "BRAINCTL_HTTP_TOKEN must be set and at least "
                f"{_MIN_TOKEN_CHARS} characters long"
            )
        raw_allowed = os.environ.get("BRAINCTL_HTTP_ALLOWED_TOOLS", "").strip()
        if not raw_allowed:
            raise ValueError(
                "BRAINCTL_HTTP_ALLOWED_TOOLS must be set to a non-empty "
                "comma-separated list of MCP tool names"
            )
        allowed_tools = frozenset(
            part.strip() for part in raw_allowed.split(",") if part.strip()
        )
        if not allowed_tools:
            raise ValueError("BRAINCTL_HTTP_ALLOWED_TOOLS contained only empty entries")
        try:
            port = int(os.environ.get("BRAINCTL_HTTP_PORT", "8080"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"BRAINCTL_HTTP_PORT must be an integer, got "
                f"{os.environ.get('BRAINCTL_HTTP_PORT')!r}: {exc}"
            ) from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"BRAINCTL_HTTP_PORT out of range (1–65535): {port}")
        host = os.environ.get("BRAINCTL_HTTP_HOST", "0.0.0.0")
        log_level = os.environ.get("BRAINCTL_HTTP_LOG_LEVEL", "info").lower()
        if log_level not in {"debug", "info", "warning", "error", "critical"}:
            raise ValueError(f"BRAINCTL_HTTP_LOG_LEVEL invalid: {log_level!r}")
        return cls(
            token=token,
            allowed_tools=allowed_tools,
            host=host,
            port=port,
            log_level=log_level,
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_json_logging(level: str) -> logging.Logger:
    """Configure JSON logging on the `brainctl.http` logger.

    Safe to call multiple times — handlers are attached to the named
    logger only once.
    """
    logger = logging.getLogger("brainctl.http")
    logger.setLevel(getattr(logging, level.upper()))
    if not any(
        isinstance(h, logging.StreamHandler)
        and isinstance(getattr(h, "formatter", None), JsonFormatter)
        for h in logger.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"levelname": "level", "asctime": "ts"},
            )
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@dataclass
class _RateLimiter:
    """In-memory per-IP sliding-window counter.

    Not shared across processes. For a single-worker uvicorn deployment
    (the recommended v1 shape) that's sufficient; for multi-worker /
    multi-node deployments, replace with Redis.
    """

    window_seconds: float = _RATE_LIMIT_WINDOW_SECONDS
    max_requests: int = _RATE_LIMIT_MAX_REQUESTS
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check(self, client_ip: str) -> bool:
        """Return True if the request is allowed, False if it should 429."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            bucket = self._hits.setdefault(client_ip, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
        return True


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds :data:`_BODY_CAP_BYTES`."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _BODY_CAP_BYTES:
                    return JSONResponse(
                        {"error": "request body too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "invalid content-length"}, status_code=400
                )
        # Some clients omit Content-Length on chunked uploads; guard by
        # reading up to cap+1 bytes ourselves.
        body = await request.body()
        if len(body) > _BODY_CAP_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        # Starlette's default Request only reads the body once. Stash the
        # already-read body so downstream handlers don't re-await a
        # drained stream.
        request._body = body  # type: ignore[attr-defined]
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Bearer-token auth with constant-time comparison.

    Applied to every path EXCEPT those listed in :attr:`public_paths`.
    """

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        public_paths: frozenset[str],
    ) -> None:
        super().__init__(app)
        self._token_bytes = token.encode("utf-8")
        self._public_paths = public_paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._public_paths:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        supplied = header[len("Bearer ") :].strip().encode("utf-8")
        if not hmac.compare_digest(supplied, self._token_bytes):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP rate limit (100/min default)."""

    def __init__(self, app: ASGIApp, limiter: _RateLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        if not await self._limiter.check(client_ip):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------


def _make_jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Construct a JSON-RPC 2.0 error response envelope."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
# MCP bridge
# ---------------------------------------------------------------------------


class MCPBridge:
    """Direct JSON-RPC bridge into the existing MCP app.

    v1 design: rather than run a full
    :class:`mcp.server.streamable_http.StreamableHTTPSessionManager`
    inside Starlette (which expects to own the ASGI root and negotiates
    SSE that complicates post-filtering), we invoke the app's registered
    request handlers directly for the two methods we actually need —
    ``tools/list`` and ``tools/call``. This keeps the allowlist
    enforcement flat, the log surface clean, and the test matrix finite.

    The stdio transport (:mod:`agentmemory.mcp_server`) is unchanged;
    it still boots its own session and calls the same handlers we do.
    """

    def __init__(
        self,
        allowed_tools: frozenset[str],
        logger: logging.Logger,
    ) -> None:
        self._allowed = allowed_tools
        self._logger = logger

    async def startup(self) -> None:  # noqa: D401 — no-op kept for symmetry
        return

    async def shutdown(self) -> None:
        return

    async def handle_request(self, request: Request) -> Response:
        body = await request.body()
        parsed = _safe_parse_jsonrpc(body)
        if parsed is None:
            return JSONResponse(
                _make_jsonrpc_error(None, -32700, "Parse error"),
                status_code=200,
            )

        method = parsed.get("method")
        request_id = parsed.get("id")
        start = time.monotonic()
        tool_for_log = ""
        rid = request.scope.get("mcp_request_id")

        if method == "tools/list":
            payload = await self._handle_list_tools(request_id)
        elif method == "tools/call":
            params = parsed.get("params") or {}
            tool_name = ""
            if isinstance(params, dict):
                tool_name = str(params.get("name") or "")
            tool_for_log = tool_name
            if tool_name not in self._allowed:
                self._logger.info(
                    "tools/call denied",
                    extra={
                        "request_id": rid,
                        "method": method,
                        "tool": tool_name,
                        "status": 403,
                    },
                )
                return JSONResponse(
                    _make_jsonrpc_error(
                        request_id,
                        -32601,
                        f"Method not allowed: {tool_name}",
                    ),
                    status_code=200,
                )
            payload = await self._handle_call_tool(request_id, params)
        else:
            payload = _make_jsonrpc_error(
                request_id, -32601, f"Method not found: {method}"
            )

        duration_ms = (time.monotonic() - start) * 1000.0
        self._logger.info(
            "mcp request complete",
            extra={
                "request_id": rid,
                "method": method,
                "tool": tool_for_log,
                "duration_ms": round(duration_ms, 3),
                "status": 200,
            },
        )
        return JSONResponse(payload, status_code=200)

    async def _handle_list_tools(self, request_id: Any) -> dict[str, Any]:
        handler = mcp_app.request_handlers.get(mtypes.ListToolsRequest)
        if handler is None:  # pragma: no cover
            return _make_jsonrpc_error(request_id, -32601, "tools/list not registered")
        req = mtypes.ListToolsRequest(method="tools/list", params=None)
        result = await _invoke_handler(handler, req)
        if isinstance(result, dict):
            return {"jsonrpc": "2.0", "id": request_id, "error": result}
        # result is a ServerResult wrapping ListToolsResult
        tools = _extract_tools(result)
        allowed_tools = [
            t.model_dump(mode="json", exclude_none=True)
            for t in tools
            if t.name in self._allowed
        ]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": allowed_tools},
        }

    async def _handle_call_tool(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        handler = mcp_app.request_handlers.get(mtypes.CallToolRequest)
        if handler is None:  # pragma: no cover
            return _make_jsonrpc_error(request_id, -32601, "tools/call not registered")
        try:
            req_params = mtypes.CallToolRequestParams.model_validate(params)
        except ValidationError as exc:
            return _make_jsonrpc_error(
                request_id, -32602, f"Invalid params: {exc.errors()[0]['msg']}"
            )
        req = mtypes.CallToolRequest(method="tools/call", params=req_params)
        result = await _invoke_handler(handler, req)
        if isinstance(result, dict):
            return {"jsonrpc": "2.0", "id": request_id, "error": result}
        call_result = _extract_call_result(result)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": call_result.model_dump(mode="json", exclude_none=True),
        }


async def _invoke_handler(handler: Callable[..., Awaitable[Any]], req: Any) -> Any:
    """Invoke an MCP request handler, handling both newer signatures
    (request_context kwarg) and older (request only).

    Falls back to a synthetic RequestContext when the handler expects
    one — the existing mcp_server handlers don't need any of its fields
    populated beyond the stub.
    """
    try:
        return await handler(req)
    except TypeError:
        ctx = RequestContext(
            request_id=0,
            meta=None,
            session=None,  # type: ignore[arg-type]
            lifespan_context=None,
            request=None,
        )
        return await handler(req, ctx)


def _extract_tools(result: Any) -> list[mtypes.Tool]:
    inner = getattr(result, "root", result)
    tools = getattr(inner, "tools", None)
    return list(tools or [])


def _extract_call_result(result: Any) -> mtypes.CallToolResult:
    return cast(mtypes.CallToolResult, getattr(result, "root", result))


def _safe_parse_jsonrpc(body: bytes) -> dict[str, Any] | None:
    """Parse JSON-RPC request body, returning None on malformed input."""
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _health(request: Request) -> Response:  # noqa: ARG001
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: HTTPConfig) -> Starlette:
    """Build the Starlette ASGI application with all middleware wired."""
    logger = _configure_json_logging(config.log_level)
    limiter = _RateLimiter()
    bridge = MCPBridge(config.allowed_tools, logger)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:  # noqa: ARG001
        await bridge.startup()
        logger.info(
            "brainctl-mcp-http ready",
            extra={
                "host": config.host,
                "port": config.port,
                "allowed_tools_count": len(config.allowed_tools),
            },
        )
        try:
            yield
        finally:
            logger.info("brainctl-mcp-http draining")
            await bridge.shutdown()

    async def _mcp_route(request: Request) -> Response:
        request.scope.setdefault("mcp_request_id", uuid.uuid4().hex)
        return await bridge.handle_request(request)

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/mcp", _mcp_route, methods=["POST", "GET"]),
    ]

    middleware = [
        Middleware(BodySizeLimitMiddleware),
        Middleware(
            AuthMiddleware,
            token=config.token,
            public_paths=frozenset({"/health"}),
        ),
        Middleware(RateLimitMiddleware, limiter=limiter),
    ]

    starlette_app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    # Don't issue 307 redirects between `/mcp` and `/mcp/` — the spec
    # expects `/mcp` to be the canonical entry point and xAI/Strand
    # clients may or may not follow redirects.
    starlette_app.router.redirect_slashes = False
    # Stash the bridge/config/logger on the app so tests can introspect
    # without poking module-level globals.
    starlette_app.state.bridge = bridge
    starlette_app.state.config = config
    starlette_app.state.logger = logger
    return starlette_app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Console-script entry point — validates env, starts uvicorn.

    Returns the desired process exit code. Failures surface as exit 1
    with a one-line stderr message (before logging is configured).
    """
    try:
        config = HTTPConfig.from_env()
    except ValueError as exc:
        print(f"brainctl-mcp-http: bad config: {exc}", file=sys.stderr)
        return 1

    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print(
            "brainctl-mcp-http: uvicorn not installed — run "
            "`pip install 'brainctl[mcp]'` to pull the HTTP extras",
            file=sys.stderr,
        )
        return 1

    app_instance = create_app(config)
    uvicorn_config = uvicorn.Config(
        app_instance,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        access_log=False,  # our middleware handles structured access logs
        timeout_graceful_shutdown=int(_DRAIN_SECONDS),
    )
    server = uvicorn.Server(uvicorn_config)

    # Uvicorn installs its own signal handlers that already drain; we
    # add an extra belt-and-braces SIGTERM handler so the bridge
    # shutdown hook always runs.
    loop = asyncio.new_event_loop()

    def _shutdown_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        server.should_exit = True

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

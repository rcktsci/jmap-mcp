"""Entry point of jmap-mcp.

One ASGI application, one port, one protocol: the MCP server (Streamable HTTP,
stateless) behind the bearer middleware. No status endpoint, no UI, no
database - the server is a proxy and nothing else.

The bearer of the request is never logged: only its short fingerprint is used
to correlate the log lines of one request.
"""

from __future__ import annotations

import asyncio
import logging

import structlog
import uvicorn

from src.config import get_settings
from src.mcp.auth import BearerAuthMiddleware
from src.mcp.server import mcp_server


def setup_logging(level: str) -> None:
    """Wire structlog into the standard library logging."""
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=resolved, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def build_app() -> BearerAuthMiddleware:
    """The whole application: MCP behind the bearer middleware.

    ``host="0.0.0.0"`` is mandatory here: with the default the SDK turns on its
    DNS-rebinding protection for localhost only and answers requests that come
    by IP with 421. The address the server listens on is a separate setting
    (``listen``, 127.0.0.1 by default).
    """
    return BearerAuthMiddleware(
        mcp_server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    )


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = structlog.get_logger(__name__)
    log.info(
        "jmap-mcp starting",
        listen=settings.listen,
        port=settings.port,
        upstream=settings.stalwart.url,
    )
    config = uvicorn.Config(
        build_app(),
        host=settings.listen,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())

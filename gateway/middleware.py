"""Gateway middleware.

``HideToolsMiddleware`` removes named tools from what clients can see *and* call.
Its motivating use is the IAM bundle: the proxied ``read-cypher`` tool must be
hidden, because it would let a client run arbitrary Cypher and bypass the
``secure-read-cypher`` IAM filter. Hiding at the gateway covers tools that come
from the mounted downstream proxy (which we can't reconfigure per tool).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext


class HideToolsMiddleware(Middleware):
    """Drop specific tool names from tools/list and reject their tools/call."""

    def __init__(self, hidden: Sequence[str]):
        self._hidden = {name for name in hidden if name}

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        if not self._hidden:
            return tools
        return [t for t in tools if t.name not in self._hidden]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", None)
        if name in self._hidden:
            raise ToolError(f"tool '{name}' is disabled in this bundle")
        return await call_next(context)

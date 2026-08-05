"""Gateway entrypoint.

Builds one FastMCP server that serves, over a single stdio endpoint, the union of:

  1. the official Neo4j MCP server's generic tools (proxied, names unchanged), and
  2. purpose-built YAML use-case tools (prefixed, e.g. ``usecase_*``).

Run it with::

    uv run neo4j-mcp-gateway
    # or
    uv run python -m gateway.server
"""

from __future__ import annotations

import atexit
import os
import signal
import sys

from fastmcp import FastMCP

from .config import Config
from .proxy import build_downstream_proxy
from .yaml_tools import Neo4jExecutor, ToolSpecError, register_yaml_tools


def _log(msg: str) -> None:
    # stdout is reserved for the MCP stdio protocol framing; log to stderr only.
    print(f"[gateway] {msg}", file=sys.stderr, flush=True)


def build_gateway(config: Config | None = None) -> FastMCP:
    """Assemble the composite gateway server (proxy + YAML tools)."""
    config = config or Config.from_env()

    gateway = FastMCP(
        name=config.server_name,
        instructions=(
            "Neo4j gateway. Generic tools (get-schema, read-cypher, write-cypher, "
            "list-gds-procedures) are proxied from the official Neo4j MCP server. "
            f"Curated, parameterized use-case tools are prefixed '{config.usecase_prefix}'."
        ),
    )

    # 1) Proxy the official downstream server and mount its tools unchanged.
    _log(f"downstream command: {config.downstream_cmd}")
    proxy = build_downstream_proxy(config)
    gateway.mount(proxy)
    _log("mounted official Neo4j MCP tools (get-schema, read-cypher, write-cypher, list-gds-procedures)")

    # 2) Discover + register YAML use-case tools.
    executor = Neo4jExecutor(config)
    atexit.register(executor.close)
    try:
        names = register_yaml_tools(gateway, config, executor)
    except ToolSpecError as exc:
        _log(f"ERROR loading YAML tools: {exc}")
        raise

    if names:
        _log(f"registered {len(names)} YAML use-case tool(s) from {config.tools_dir}: {', '.join(names)}")
    else:
        _log(f"no YAML use-case tools found in {config.tools_dir}")

    return gateway


def _install_fast_shutdown() -> None:
    """Make Ctrl+C (SIGINT) shut the gateway down in one keypress.

    The downstream official server is a child process. On a plain Ctrl+C,
    asyncio starts a *graceful* async unwind that blocks waiting for that child
    to exit on its own — which is why it takes several Ctrl+C to actually stop.

    Instead we install our own SIGINT/SIGTERM handlers that exit immediately.
    Exiting closes our end of the pipe to the child, so the downstream sees EOF
    on stdin and shuts itself down cleanly (the same path MCP clients use when
    they stop the server). ``os._exit`` avoids re-entering the hanging async
    teardown. Signal handlers can only be installed from the main thread, which
    is where this runs.
    """

    def _handler(signum, _frame):  # noqa: ANN001 - signal handler signature
        _log(f"received signal {signum}; shutting down")
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handler)


def main() -> None:
    """Console-script / module entrypoint: build the gateway and serve over stdio."""
    gateway = build_gateway()
    _install_fast_shutdown()
    _log("serving on stdio (Ctrl+C to stop)")
    gateway.run()  # transport defaults to stdio


if __name__ == "__main__":
    main()

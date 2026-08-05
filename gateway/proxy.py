"""Downstream proxy: spawn the official Neo4j MCP server and re-expose its tools.

The gateway is an MCP *client* to the official Neo4j server (spawned over stdio)
and an MCP *server* to the editor. We do NOT reimplement the generic tools
(get-schema / read-cypher / write-cypher / list-gds-procedures) — we run the
supported server as a downstream child process and proxy it through unchanged.

See https://github.com/neo4j/mcp for the official server (a Go binary shipped as
the ``neo4j-mcp-server`` PyPI wheel and the ``neo4j/mcp`` Docker image).
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import ProxyClient

from .config import Config


def build_downstream_proxy(config: Config) -> FastMCP:
    """Create a FastMCP proxy in front of the official Neo4j MCP server.

    The proxy lazily spawns the downstream process (via ``NEO4J_MCP_CMD``) when
    the first request arrives and forwards tool listing/calls to it. Neo4j
    credentials are injected into the child's environment, so the same creds
    drive both the downstream server and our own YAML executor.
    """
    argv = config.downstream_argv
    command, args = argv[0], argv[1:]

    # The downstream child inherits our stderr by default, so its logs are
    # visible when debugging the gateway (stdout is reserved for MCP framing).
    transport = StdioTransport(
        command=command,
        args=args,
        env=config.downstream_env(),
    )

    # ProxyClient forwards advanced MCP features (sampling, elicitation, logging,
    # progress) between the editor and the downstream server, not just tool calls.
    proxy = create_proxy(ProxyClient(transport), name="neo4j-official")
    return proxy

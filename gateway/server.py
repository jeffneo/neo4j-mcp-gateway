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

from .bundles import list_bundles
from .config import BUNDLES_DIR, Config, resolve_configs
from .audit import (ENV_PATH, AuditMiddleware, AuditSink, audit_path,
                    build_forwarder, checkpoint_every, include_arguments)
from .middleware import HideToolsMiddleware
from .proxy import build_downstream_proxy
from .pytools import load_pytools
from .security_tools import build_security_tools
from .yaml_tools import Neo4jExecutor, ToolSpecError, register_yaml_tools


def _log(msg: str) -> None:
    # stdout is reserved for the MCP stdio protocol framing; log to stderr only.
    print(f"[gateway] {msg}", file=sys.stderr, flush=True)


def _check_connection_safety(configs: list[Config]) -> None:
    """Refuse to serve an open bundle and a mediated bundle from one database.

    Enforcement binds to the *connection*, not the bundle name. If an open bundle
    shares a datasource with a mediated one, its unfiltered tools read the same
    rows the mediated bundle is protecting — the entitlement guarantee is void.
    Hiding tools cannot fix this, so it is a hard error rather than a warning.
    """
    by_connection: dict[tuple[str, str, str], list[Config]] = {}
    for cfg in configs:
        by_connection.setdefault(cfg.connection_key, []).append(cfg)

    for key, group in by_connection.items():
        modes = {c.security.mode for c in group}
        if len(modes) > 1:
            uri, user, database = key
            names = ", ".join(f"{c.active_bundle}({c.security.mode})" for c in group)
            raise SystemExit(
                f"[gateway] REFUSING TO START: bundles with different security modes share one "
                f"database.\n"
                f"           datasource: {uri} db={database} user={user}\n"
                f"           bundles:    {names}\n"
                f"           An open bundle's unfiltered tools can read the rows the mediated "
                f"bundle protects.\n"
                f"           Fix: give them separate databases/instances (per-bundle .env), make "
                f"both mediated, or serve them from separate gateway processes."
            )



def _audit_middleware(configs: list[Config]) -> list:
    """Build the audit middleware, or refuse to start if a bundle requires it.

    ``security.require_audit`` is the same fail-closed stance as
    ``security.mode``: running an entitlement-mediated bundle with no trail
    becomes a recorded decision rather than something that happens by omission.
    """
    env = configs[0].env_snapshot
    path = audit_path(env)
    requiring = [c.active_bundle for c in configs if c.security.require_audit]
    if requiring and not path:
        raise SystemExit(
            f"[gateway] bundle(s) {', '.join(requiring)} declare security.require_audit, "
            f"but no audit log is configured. Set {ENV_PATH}=/path/to/audit.jsonl, or "
            "remove require_audit to run unaudited deliberately.")
    if not path:
        return []
    args = include_arguments(env)
    forwarder = build_forwarder(env)
    every = checkpoint_every(env)
    sink = AuditSink(path, forwarder=forwarder, checkpoint_every=every)
    atexit.register(sink.close)
    _log(f"audit log: {path}" + ("  (including argument values)" if args else ""))
    if forwarder is None:
        _log("  WARNING: no NEO4J_MCP_AUDIT_FORWARDER — records are hash-chained, but "
             "nothing anchors the chain head off this host, so wholesale truncation "
             "and rewriting stay undetectable")
    else:
        _log(f"  chain checkpoints every {every} records -> {type(forwarder).__name__}")
    return [AuditMiddleware(sink, configs, log_arguments=args)]


def _compose_instructions(configs: list[Config]) -> str:
    """Merge each bundle's model-facing instructions under a namespacing preamble."""
    if len(configs) == 1:
        return configs[0].instructions
    parts = [
        "This gateway serves several use cases at once. Tools are namespaced by "
        "bundle: a tool named '<bundle>_...' belongs to that bundle and reads that "
        "bundle's database. Use the section below matching the user's question, and "
        "do not mix tools across bundles in a single line of reasoning.",
    ]
    for cfg in configs:
        parts.append(f"## {cfg.active_bundle} (tools prefixed '{cfg.active_bundle}_')\n{cfg.instructions}")
    return "\n\n".join(parts)


def build_gateway(config: Config | None = None) -> FastMCP:
    """Assemble the composite gateway server for one or more bundles."""
    configs = [config] if config is not None else resolve_configs()
    if len(configs) > 1:
        return _build_multi_gateway(configs)
    config = configs[0]

    _log(f"active bundle: {config.active_bundle}  ({config.bundle.description or 'no description'})")
    if config.security.mediated:
        posture = ("mediated (exploration: curated tools + open-ended secure-read-cypher)"
                   if config.security.expose_open_query_tool
                   else "mediated (curated only: no open-ended query tool)")
    else:
        posture = "open (no entitlement filtering)"
    _log(f"security posture: {posture}")

    # Under mediation, the proxied raw read tool would bypass the entitlement
    # filter entirely, so it is hidden by default rather than by convention.
    hidden = list(config.hide_tools)
    if config.security.mediated and not config.security.allow_unmediated_read:
        if "read-cypher" not in hidden:
            hidden.append("read-cypher")
    elif config.security.mediated:
        _log("WARNING: security.allow_unmediated_read=true — raw read-cypher is exposed "
             "and bypasses entitlement filtering")

    middleware = _audit_middleware([config])
    if hidden:
        middleware.append(HideToolsMiddleware(hidden))
    gateway = FastMCP(name=config.server_name, instructions=config.instructions, middleware=middleware)

    # 1) Proxy the official downstream server and mount its tools unchanged.
    _log(f"downstream command: {config.downstream_cmd}  (database: {config.neo4j_database})")
    proxy = build_downstream_proxy(config)
    gateway.mount(proxy)
    _log("mounted official Neo4j MCP tools (get-schema, read-cypher, write-cypher, list-gds-procedures)")
    if hidden:
        _log(f"hiding proxied tool(s): {', '.join(hidden)}")

    executor = Neo4jExecutor(config)
    atexit.register(executor.close)

    # 2) Discover + register YAML use-case tools.
    try:
        names = register_yaml_tools(gateway, config, executor)
    except ToolSpecError as exc:
        _log(f"ERROR loading YAML tools: {exc}")
        raise
    if names:
        _log(f"registered {len(names)} YAML tool(s) from {config.tools_dir}: {', '.join(names)}")
    else:
        _log(f"no YAML tools in {config.tools_dir}")

    # 3) Register code-backed (Python) tools, if the bundle ships any.
    pytools, used_raw = load_pytools(config, executor)
    for tool in pytools:
        gateway.add_tool(tool)
    if pytools:
        _log(f"registered {len(pytools)} code tool(s) from {config.pytools_dir}: "
             f"{', '.join(t.name for t in pytools)}")
    if used_raw and config.security.mediated:
        _log("WARNING: a code tool in this bundle used the raw executor — that path is NOT "
             "entitlement-filtered. Use ToolContext.secure_run() for business records.")

    # 4) Engine security tools for a mediated bundle (resolve-identity, and the
    #    open-ended secure-read-cypher unless the bundle publishes curated only).
    security_tools = build_security_tools(config, executor)
    for tool in security_tools:
        gateway.add_tool(tool)
    if security_tools:
        _log(f"registered {len(security_tools)} security tool(s): "
             f"{', '.join(t.name for t in security_tools)}")

    return gateway


def _build_multi_gateway(configs: list[Config]) -> FastMCP:
    """Serve several bundles from one endpoint, each with its own connection.

    Every bundle keeps an independent driver, so bundles may sit on different
    databases or entirely different Neo4j instances. Tools are namespaced by
    bundle name; the downstream official server is shared by bundles that point
    at the same datasource rather than spawned once per bundle.
    """
    _check_connection_safety(configs)

    names = ", ".join(c.active_bundle for c in configs)
    _log(f"active bundles ({len(configs)}): {names}")

    # Hidden names must carry each bundle's prefix, since mounts are namespaced.
    hidden: list[str] = []
    for cfg in configs:
        prefix = f"{cfg.active_bundle}_"
        for tool in cfg.hide_tools:
            hidden.append(f"{prefix}{tool}")
        if cfg.security.mediated and not cfg.security.allow_unmediated_read:
            if f"{prefix}read-cypher" not in hidden:
                hidden.append(f"{prefix}read-cypher")

    middleware = _audit_middleware(configs)
    if hidden:
        middleware.append(HideToolsMiddleware(hidden))
    gateway = FastMCP(
        name=os.getenv("GATEWAY_NAME") or "neo4j-mcp-gateway",
        instructions=_compose_instructions(configs),
        middleware=middleware,
    )

    # One downstream child per distinct datasource. Bundles sharing a connection
    # share its generic tools instead of spawning a redundant server each.
    downstreams: dict[tuple[str, str, str], str] = {}

    for cfg in configs:
        prefix = f"{cfg.active_bundle}_"
        posture = cfg.security.mode + (
            "" if not cfg.security.mediated
            else (" (exploration)" if cfg.security.expose_open_query_tool else " (curated only)")
        )
        _log(f"  [{cfg.active_bundle}] posture={posture} db={cfg.neo4j_database} uri={cfg.neo4j_uri}")

        if cfg.connection_key not in downstreams:
            gateway.mount(build_downstream_proxy(cfg), prefix=cfg.active_bundle)
            downstreams[cfg.connection_key] = cfg.active_bundle
            _log(f"  [{cfg.active_bundle}] mounted official tools as {prefix}get-schema, …")
        else:
            owner = downstreams[cfg.connection_key]
            _log(f"  [{cfg.active_bundle}] shares {owner}'s datasource — reusing {owner}_* generic tools")

        executor = Neo4jExecutor(cfg)
        atexit.register(executor.close)

        try:
            registered = register_yaml_tools(gateway, cfg, executor, prefix=prefix)
        except ToolSpecError as exc:
            _log(f"ERROR loading YAML tools for '{cfg.active_bundle}': {exc}")
            raise
        if registered:
            _log(f"  [{cfg.active_bundle}] {len(registered)} YAML tool(s): {', '.join(registered)}")

        pytools, used_raw = load_pytools(cfg, executor)
        for tool in pytools:
            # Namespace code tools too, or two bundles' tools would collide.
            gateway.add_tool(tool.model_copy(update={"name": f"{prefix}{tool.name}"}))
        if pytools:
            _log(f"  [{cfg.active_bundle}] {len(pytools)} code tool(s)")
        if used_raw and cfg.security.mediated:
            _log(f"  WARNING [{cfg.active_bundle}]: a code tool used the raw executor — that path "
                 "is NOT entitlement-filtered.")

        for tool in build_security_tools(cfg, executor, prefix):
            gateway.add_tool(tool)
            _log(f"  [{cfg.active_bundle}] security tool: {tool.name}")

    if hidden:
        _log(f"hiding proxied tool(s): {', '.join(hidden)}")
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
    """Console-script / module entrypoint: build the gateway and serve over stdio.

    Flags:
      --list-bundles      print available bundles and exit
      --bundle <name>     select the active bundle (same as ACTIVE_BUNDLE=<name>)
    """
    argv = sys.argv[1:]

    if "--list-bundles" in argv:
        for b in list_bundles(BUNDLES_DIR):
            print(f"{b.name:20}  {b.description}")
        return

    if "--bundle" in argv:
        i = argv.index("--bundle")
        if i + 1 < len(argv):
            os.environ["ACTIVE_BUNDLE"] = argv[i + 1]

    gateway = build_gateway()
    _install_fast_shutdown()
    _log("serving on stdio (Ctrl+C to stop)")
    gateway.run()  # transport defaults to stdio


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Run a single YAML use-case tool directly against Neo4j — no MCP, no restart.

This is the fast inner loop for developing tools: it loads the same `.env` and
`tools/` directory the gateway uses, executes one tool's Cypher through the same
code path (`gateway.yaml_tools`), and prints the JSON result. No MCP client, no
gateway restart, no tool-list caching to fight.

Usage:
    uv run python scripts/try_tool.py --list
    uv run python scripts/try_tool.py <tool_name> [param=value ...]

Examples:
    uv run python scripts/try_tool.py customer_sessions customer_id=CUST-1004
    uv run python scripts/try_tool.py mule_hubs min_victims=2
    uv run python scripts/try_tool.py ato_session_triage min_risk=5 limit=10

The tool name may be given with or without the `usecase_` prefix. Point at a
different tools directory (e.g. the reference answers) with TOOLS_DIR:
    TOOLS_DIR=solutions uv run python scripts/try_tool.py mule_hubs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `gateway` importable even when run as `python scripts/try_tool.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config
from gateway.yaml_tools import (Neo4jExecutor, ParamSpec, ToolSpec, load_tool_specs,
                                resolve_tool_query)


def _coerce(raw: str, ptype: str):
    """Turn a command-line string into the parameter's declared type."""
    if ptype == "integer":
        return int(raw)
    if ptype == "number":
        return float(raw)
    if ptype == "boolean":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if ptype in ("array", "object"):
        return json.loads(raw)
    return raw  # string


def _print_list(specs: list[ToolSpec], prefix: str) -> None:
    print("Available tools (from tools/ dir):\n")
    for s in sorted(specs, key=lambda x: x.name):
        args = " ".join(
            f"{p.name}=<{p.type}>" if p.required else f"[{p.name}=<{p.type}>]"
            for p in s.parameters
        )
        print(f"  {prefix}{s.name} {args}".rstrip())
    print("\nRun one with:  uv run python scripts/try_tool.py <name> key=value ...")


def main(argv: list[str]) -> int:
    config = Config.from_env()

    try:
        specs = load_tool_specs(config.tools_dir)
    except Exception as exc:  # a malformed YAML file — the point of the fast loop
        print(f"error loading tools from {config.tools_dir}:\n  {exc}", file=sys.stderr)
        return 1

    by_name = {s.name: s for s in specs}

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] in ("--list", "-l"):
        _print_list(specs, config.usecase_prefix)
        return 0

    name = argv[0]
    if name.startswith(config.usecase_prefix):
        name = name[len(config.usecase_prefix):]

    spec = by_name.get(name)
    if spec is None:
        print(f"unknown tool {name!r} in {config.tools_dir}", file=sys.stderr)
        _print_list(specs, config.usecase_prefix)
        return 1

    # Parse key=value args and coerce to declared types.
    pmap: dict[str, ParamSpec] = {p.name: p for p in spec.parameters}
    params: dict[str, object] = {p.name: p.default for p in spec.parameters if p.has_default}
    principal: str | None = None
    for arg in argv[1:]:
        key, sep, value = arg.partition("=")
        if not sep:
            print(f"ignoring malformed arg {arg!r} (expected key=value)", file=sys.stderr)
            continue
        if key == "principal":
            # In a mediated bundle, run the tool as this caller (needs impersonation).
            principal = value
            continue
        if key not in pmap:
            print(f"warning: {key!r} is not a parameter of {spec.name!r}", file=sys.stderr)
            continue
        params[key] = _coerce(value, pmap[key].type)

    missing = [p.name for p in spec.parameters if p.required and p.name not in params]
    if missing:
        print(f"missing required parameter(s): {', '.join(missing)}", file=sys.stderr)
        _print_list([spec], config.usecase_prefix)
        return 1

    executor = Neo4jExecutor(config)
    try:
        query, extra = resolve_tool_query(config, spec, principal)
        params.update(extra)
        rows = executor.run(query, params, spec.read_only)
    except Exception as exc:  # Neo4j / Cypher errors — show them plainly
        print(f"query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        executor.close()

    payload = {"tool": spec.name, "read_only": spec.read_only,
               "params": {k: v for k, v in params.items() if not k.startswith("__secure_")},
               "count": len(rows), "records": rows}
    if config.security.mediated:
        payload["security"] = {"mode": "mediated", "principal": principal or "(from environment)"}
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

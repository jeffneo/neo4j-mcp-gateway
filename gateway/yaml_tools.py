"""YAML use-case tools: discovery, validation, registration, and execution.

Each ``*.yaml`` file under the tools directory declares one purpose-built tool:

    name: search_movies_by_actor
    description: Find movies a given actor appeared in.
    parameters:
      - name: actor
        type: string
        description: Full name of the actor
        required: true
    cypher: |
      MATCH (p:Person {name: $actor})-[:ACTED_IN]->(m:Movie)
      RETURN m.title AS title, m.released AS year
      ORDER BY year
    read_only: true

Adding a tool is: drop a new YAML file in ``tools/`` and restart the gateway.

This module keeps the registry cleanly separated from the driver and the server
so that a retrieval/routing layer (e.g. vector search over many tools) could
later sit in front of :func:`load_tool_specs` without touching execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import neo4j
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from .config import Config

# Map the small YAML type vocabulary to JSON Schema types. Anything not listed
# falls back to "string" (with a clear validation error raised at load time).
_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "str": "string",
    "integer": "integer",
    "int": "integer",
    "number": "number",
    "float": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "array": "array",
    "list": "array",
    "object": "object",
    "dict": "object",
}


class ToolSpecError(ValueError):
    """Raised when a YAML tool file is malformed. The message names the file."""


@dataclass
class ParamSpec:
    name: str
    type: str  # JSON Schema type
    description: str
    required: bool
    default: Any = None
    has_default: bool = False


@dataclass
class ToolSpec:
    """A validated, in-memory representation of one YAML tool file."""

    name: str
    description: str
    parameters: list[ParamSpec]
    cypher: str
    read_only: bool
    source_path: Path

    def input_schema(self) -> dict[str, Any]:
        """Build the MCP ``inputSchema`` (JSON Schema) from the parameter list."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.has_default:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema


# --------------------------------------------------------------------------- #
# Discovery + validation
# --------------------------------------------------------------------------- #

def _require(cond: bool, path: Path, msg: str) -> None:
    if not cond:
        raise ToolSpecError(f"{path.name}: {msg}")


def parse_tool_spec(path: Path) -> ToolSpec:
    """Parse and validate a single YAML tool file into a :class:`ToolSpec`."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ToolSpecError(f"{path.name}: invalid YAML: {exc}") from exc

    _require(isinstance(raw, dict), path, "top level must be a mapping")

    name = raw.get("name")
    _require(isinstance(name, str) and name.strip() != "", path, "'name' is required and must be a non-empty string")
    _require(
        all(c.isalnum() or c == "_" for c in name),
        path,
        f"'name' must be alphanumeric/underscore only (got {name!r})",
    )

    description = raw.get("description")
    _require(
        isinstance(description, str) and description.strip() != "",
        path,
        "'description' is required and must be a non-empty string",
    )

    cypher = raw.get("cypher")
    _require(isinstance(cypher, str) and cypher.strip() != "", path, "'cypher' is required and must be a non-empty string")

    read_only = raw.get("read_only", True)
    _require(isinstance(read_only, bool), path, "'read_only' must be a boolean if present")

    raw_params = raw.get("parameters", []) or []
    _require(isinstance(raw_params, list), path, "'parameters' must be a list if present")

    params: list[ParamSpec] = []
    seen: set[str] = set()
    for i, rp in enumerate(raw_params):
        _require(isinstance(rp, dict), path, f"parameter #{i + 1} must be a mapping")
        pname = rp.get("name")
        _require(isinstance(pname, str) and pname.strip() != "", path, f"parameter #{i + 1} needs a 'name'")
        _require(pname not in seen, path, f"duplicate parameter name {pname!r}")
        seen.add(pname)

        ptype_raw = str(rp.get("type", "string")).lower()
        _require(
            ptype_raw in _TYPE_MAP,
            path,
            f"parameter {pname!r} has unsupported type {ptype_raw!r} (allowed: {sorted(set(_TYPE_MAP))})",
        )
        pdesc = rp.get("description", "")
        _require(isinstance(pdesc, str), path, f"parameter {pname!r} 'description' must be a string")
        prequired = rp.get("required", False)
        _require(isinstance(prequired, bool), path, f"parameter {pname!r} 'required' must be a boolean")

        has_default = "default" in rp
        params.append(
            ParamSpec(
                name=pname,
                type=_TYPE_MAP[ptype_raw],
                description=pdesc,
                required=bool(prequired),
                default=rp.get("default"),
                has_default=has_default,
            )
        )

    return ToolSpec(
        name=name,
        description=description.strip(),
        parameters=params,
        cypher=cypher,
        read_only=bool(read_only),
        source_path=path,
    )


def load_tool_specs(tools_dir: Path) -> list[ToolSpec]:
    """Discover and validate every ``*.yaml`` / ``*.yml`` file in ``tools_dir``.

    Raises :class:`ToolSpecError` on the first malformed file so problems surface
    loudly at startup rather than silently dropping a tool.
    """
    if not tools_dir.exists():
        return []
    files = sorted(p for p in tools_dir.iterdir() if p.suffix.lower() in {".yaml", ".yml"})
    specs = [parse_tool_spec(p) for p in files]

    # Guard against two files declaring the same tool name.
    names: dict[str, Path] = {}
    for spec in specs:
        if spec.name in names:
            raise ToolSpecError(
                f"duplicate tool name {spec.name!r} in {spec.source_path.name} "
                f"(already defined by {names[spec.name].name})"
            )
        names[spec.name] = spec.source_path
    return specs


# --------------------------------------------------------------------------- #
# Neo4j execution
# --------------------------------------------------------------------------- #

def _to_jsonable(value: Any) -> Any:
    """Convert Neo4j driver values into plain JSON-serializable Python objects.

    Handles temporal types, spatial points, and graph entities (Node /
    Relationship / Path) so results always serialize cleanly to JSON.
    """
    # Graph entities
    if isinstance(value, neo4j.graph.Node):
        return {
            "_id": value.element_id,
            "_labels": sorted(value.labels),
            **{k: _to_jsonable(v) for k, v in dict(value).items()},
        }
    if isinstance(value, neo4j.graph.Relationship):
        return {
            "_id": value.element_id,
            "_type": value.type,
            "_start": value.start_node.element_id if value.start_node else None,
            "_end": value.end_node.element_id if value.end_node else None,
            **{k: _to_jsonable(v) for k, v in dict(value).items()},
        }
    if isinstance(value, neo4j.graph.Path):
        return {
            "nodes": [_to_jsonable(n) for n in value.nodes],
            "relationships": [_to_jsonable(r) for r in value.relationships],
        }
    # Containers
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    # Primitives pass through; everything else (temporal, spatial, bytes, ...)
    # is rendered via its string form, which the Neo4j types implement as ISO-8601
    # for temporals and WKT-like for points.
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Neo4jExecutor:
    """Thin wrapper around the Neo4j driver used by YAML tools.

    The driver is created lazily on first use so the gateway can start (and list
    tools) even when Neo4j is temporarily unreachable — connection problems then
    surface as clean per-call tool errors instead of a startup crash.
    """

    def __init__(self, config: Config):
        self._config = config
        self._driver: neo4j.Driver | None = None

    def _get_driver(self) -> neo4j.Driver:
        if self._driver is None:
            self._driver = neo4j.GraphDatabase.driver(
                self._config.neo4j_uri,
                auth=(self._config.neo4j_username, self._config.neo4j_password),
            )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def run(self, cypher: str, params: dict[str, Any], read_only: bool) -> list[dict[str, Any]]:
        """Execute ``cypher`` with ``$params`` in the declared access mode.

        Uses managed transactions (``execute_read`` / ``execute_write``) so the
        driver routes to the right cluster member and retries transient errors.
        """
        driver = self._get_driver()

        def _work(tx: neo4j.ManagedTransaction) -> list[dict[str, Any]]:
            result = tx.run(cypher, params)
            return [ {k: _to_jsonable(v) for k, v in record.items()} for record in result ]

        with driver.session(database=self._config.neo4j_database) as session:
            if read_only:
                return session.execute_read(_work)
            return session.execute_write(_work)


# --------------------------------------------------------------------------- #
# MCP registration
# --------------------------------------------------------------------------- #

def _make_handler(spec: ToolSpec, executor: Neo4jExecutor):
    """Build the async tool handler that binds arguments to the Cypher params."""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        # Start from declared defaults, then overlay caller-supplied arguments.
        params: dict[str, Any] = {p.name: p.default for p in spec.parameters if p.has_default}
        params.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            rows = executor.run(spec.cypher, params, spec.read_only)
        except neo4j.exceptions.Neo4jError as exc:
            # Cypher / server-side errors -> clean tool error (code + message).
            raise ToolError(f"Neo4j error [{exc.code}]: {exc.message}") from exc
        except neo4j.exceptions.DriverError as exc:
            # Connection / auth / config errors.
            raise ToolError(f"Neo4j driver error: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surface anything else cleanly
            raise ToolError(f"Tool '{spec.name}' failed: {exc}") from exc

        # Structured payload; FastMCP also serializes this to JSON text content.
        return {"count": len(rows), "records": rows}

    return handler


def build_yaml_tool(spec: ToolSpec, executor: Neo4jExecutor, prefix: str) -> FunctionTool:
    """Turn a :class:`ToolSpec` into a registered-ready :class:`FunctionTool`."""
    mode = "read" if spec.read_only else "write"
    description = (
        f"{spec.description}\n\n"
        f"(Use-case tool from {spec.source_path.name}; runs curated Cypher in {mode} mode.)"
    )
    return FunctionTool(
        name=f"{prefix}{spec.name}",
        description=description,
        parameters=spec.input_schema(),
        fn=_make_handler(spec, executor),
    )


def register_yaml_tools(mcp: FastMCP, config: Config, executor: Neo4jExecutor) -> list[str]:
    """Discover YAML tools and register each on ``mcp``. Returns the tool names.

    Every tool name is prefixed (``usecase_`` by default) so use-case tools are
    namespaced distinctly from the proxied official tools and can never collide.
    """
    specs = load_tool_specs(config.tools_dir)
    registered: list[str] = []
    for spec in specs:
        tool = build_yaml_tool(spec, executor, config.usecase_prefix)
        mcp.add_tool(tool)
        registered.append(tool.name)
    return registered

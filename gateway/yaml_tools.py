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
from dataclasses import dataclass, field
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
    """A validated, in-memory representation of one YAML tool file.

    Two authoring forms:

    * **direct** — a single ``cypher:`` block. Used by ``open`` bundles.
    * **mediated** — ``match:`` + ``scope:`` + ``return:``. Required by
      ``mediated`` bundles so the engine can insert the authorization prelude and
      entitlement filter *between* the match and the return. We never parse Cypher
      to find the RETURN: getting that wrong would be a security bug, so the split
      is declared by the author instead.
    """

    name: str
    description: str
    parameters: list[ParamSpec]
    cypher: str                      # direct form (empty when mediated)
    read_only: bool
    source_path: Path
    match_clause: str = ""           # mediated form
    return_clause: str = ""
    scope: list[str] = field(default_factory=list)
    protect: list[str] = field(default_factory=list)
    # Optional arguments used only by scripts/validate_bundle.py, so a tool with
    # required parameters can still be exercised (and persona-diffed) in CI.
    sample_args: dict[str, Any] = field(default_factory=dict)

    @property
    def is_mediated_form(self) -> bool:
        return bool(self.match_clause)

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

    # Either the direct form (cypher:) or the mediated form (match: + return:).
    cypher = raw.get("cypher")
    match_clause = raw.get("match")
    return_clause = raw.get("return")
    has_direct = isinstance(cypher, str) and cypher.strip() != ""
    has_mediated = isinstance(match_clause, str) and match_clause.strip() != ""

    _require(
        has_direct or has_mediated,
        path,
        "a tool needs either 'cypher' (direct) or 'match' + 'return' (mediated form)",
    )
    _require(
        not (has_direct and has_mediated),
        path,
        "use either 'cypher' or 'match'/'return', not both",
    )

    scope: list[str] = []
    protect: list[str] = []
    if has_mediated:
        _require(
            isinstance(return_clause, str) and return_clause.strip() != "",
            path,
            "the mediated form requires a 'return' clause (it runs AFTER entitlement filtering)",
        )
        raw_scope = raw.get("scope") or []
        _require(isinstance(raw_scope, list) and raw_scope, path,
                 "the mediated form requires 'scope': the variables carried from 'match' into 'return'")
        raw_protect = raw.get("protect") or []
        _require(isinstance(raw_protect, list), path, "'protect' must be a list if present")
        scope = [str(v) for v in raw_scope]
        protect = [str(v) for v in raw_protect]
        for v in scope + protect:
            _require(v.isidentifier(), path, f"variable {v!r} is not a valid Cypher identifier")
        unknown = [v for v in protect if v not in scope]
        _require(not unknown, path, f"'protect' lists variables missing from 'scope': {unknown}")

    sample_args = raw.get("sample_args") or {}
    _require(isinstance(sample_args, dict), path, "'sample_args' must be a mapping if present")

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
        cypher=cypher if has_direct else "",
        read_only=bool(read_only),
        source_path=path,
        match_clause=match_clause if has_mediated else "",
        return_clause=return_clause if has_mediated else "",
        scope=scope,
        protect=protect,
        sample_args=dict(sample_args),
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
                # The IAM auth prelude probes many optional property keys by design;
                # silence the resulting "property key does not exist" notifications.
                notifications_min_severity="OFF",
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

def resolve_tool_query(
    config: Config | None, spec: ToolSpec, principal: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Return ``(query, extra_params)`` for a tool spec.

    Direct-form tools run their ``cypher`` as-is. In a mediated bundle, a
    mediated-form tool is composed with the authorization prelude and entitlement
    filter, and the caller's principal is resolved. Shared by the MCP handler and
    the dev/validation scripts so all three take the same path.
    """
    policy = config.security if config else None
    if not (policy and policy.mediated and spec.is_mediated_form):
        return spec.cypher, {}

    from . import mediation

    resolved, _ = mediation.resolve_principal(policy, principal)
    query = mediation.compose(
        policy, spec.match_clause, spec.scope, spec.return_clause.strip(), spec.protect
    )
    return query, mediation.security_params(policy, resolved)


def _make_handler(spec: ToolSpec, executor: Neo4jExecutor, config: Config | None = None):
    """Build the async tool handler that binds arguments to the Cypher params.

    In a mediated bundle the tool's ``match``/``return`` clauses are composed with
    the authorization prelude and entitlement filter, and the caller's principal is
    resolved per call — so the same curated tool returns different rows per user.
    """
    async def handler(**kwargs: Any) -> dict[str, Any]:
        # Start from declared defaults, then overlay caller-supplied arguments.
        params: dict[str, Any] = {p.name: p.default for p in spec.parameters if p.has_default}
        params.update({k: v for k, v in kwargs.items() if v is not None and k != "principal"})

        query, extra = resolve_tool_query(config, spec, kwargs.get("principal"))
        params.update(extra)

        try:
            rows = executor.run(query, params, spec.read_only)
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


def _check_mediated_spec(spec: ToolSpec) -> None:
    """A mediated bundle's tools must be filterable. Fail loudly at load, not later."""
    if not spec.is_mediated_form:
        raise ToolSpecError(
            f"{spec.source_path.name}: this bundle declares security.mode=mediated, so tools must "
            "use the mediated form so the entitlement filter can be inserted between the match and "
            "the return. Replace 'cypher:' with:\n"
            "    match: |\n      MATCH ...        # no RETURN\n"
            "    scope: [x]         # variables carried into the return\n"
            "    return: |\n      RETURN ...       # runs AFTER filtering"
        )
    if not spec.read_only:
        raise ToolSpecError(
            f"{spec.source_path.name}: mediated bundles are read-only "
            "(entitlement mediation covers reads; a write tool would be unfiltered)."
        )


def build_yaml_tool(
    spec: ToolSpec, executor: Neo4jExecutor, prefix: str, config: Config | None = None
) -> FunctionTool:
    """Turn a :class:`ToolSpec` into a registered-ready :class:`FunctionTool`."""
    policy = config.security if config else None
    mediated = bool(policy and policy.mediated)

    if mediated:
        note = ("Results are filtered to the caller's entitlements; aggregates are computed after "
                "filtering, so they reflect only rows the caller may see.")
    else:
        mode = "read" if spec.read_only else "write"
        note = f"Runs curated Cypher in {mode} mode."
    description = f"{spec.description}\n\n({spec.source_path.name} — {note})"

    schema = spec.input_schema()
    # Under mediation with impersonation enabled, every curated tool can also be
    # run as another principal — that is what makes a persona-diff demo possible.
    if mediated and config is not None:
        from . import mediation

        if mediation.impersonation_allowed(policy):
            schema["properties"]["principal"] = {
                "type": "string",
                "description": "Run as this principal (test impersonation; enabled for this deployment).",
            }

    return FunctionTool(
        name=f"{prefix}{spec.name}",
        description=description,
        parameters=schema,
        fn=_make_handler(spec, executor, config),
    )


def register_yaml_tools(mcp: FastMCP, config: Config, executor: Neo4jExecutor) -> list[str]:
    """Discover YAML tools and register each on ``mcp``. Returns the tool names.

    Every tool name is prefixed (``usecase_`` by default) so use-case tools are
    namespaced distinctly from the proxied official tools and can never collide.
    """
    specs = load_tool_specs(config.tools_dir)
    registered: list[str] = []
    for spec in specs:
        if config.security.mediated:
            _check_mediated_spec(spec)
        tool = build_yaml_tool(spec, executor, config.usecase_prefix, config)
        mcp.add_tool(tool)
        registered.append(tool.name)
    return registered

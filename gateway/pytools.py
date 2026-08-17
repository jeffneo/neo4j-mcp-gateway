"""Code-backed bundle tools.

Some tools can't be expressed as static parameterized Cypher (a YAML tool) —
they need logic: composing a query at runtime, validating caller input, or
resolving identity from the environment. The IAM bundle's ``secure-read-cypher``
is the canonical example.

A bundle supplies these as Python modules under ``bundles/<name>/pytools/``.
Each module defines::

    def build_tools(ctx: ToolContext) -> list[Tool]:
        ...

and returns fully-formed FastMCP tools (typically ``FunctionTool`` instances).
``ctx`` gives the module the resolved :class:`~gateway.config.Config` and a live
:class:`~gateway.yaml_tools.Neo4jExecutor` so handlers can run Cypher.

Unlike YAML tools, pytools own their tool name verbatim (no ``usecase_`` prefix)
— so a bundle can register domain primitives like ``resolve-identity``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from fastmcp.tools.tool import Tool

from .config import Config
from .yaml_tools import Neo4jExecutor


class ToolContext:
    """What a pytool module's ``build_tools`` receives.

    Use :meth:`secure_run` in a mediated bundle: the engine cannot auto-wrap
    arbitrary Python, so a code tool that reads business records must opt into
    mediation deliberately. Reaching for :attr:`executor` directly is allowed
    (reference data, admin metadata) but is *recorded*, and the server logs a
    warning naming any bundle whose code tools bypassed mediation — an unmediated
    path should be visible rather than assumed safe.
    """

    def __init__(self, config: Config, executor: Neo4jExecutor):
        self.config = config
        self._executor = executor
        self.used_raw_executor = False

    @property
    def executor(self) -> Neo4jExecutor:
        """The raw executor — no entitlement filtering. Use knowingly."""
        self.used_raw_executor = True
        return self._executor

    def secure_run(
        self,
        match_clause: str,
        scope: list[str],
        final_return: str = "",
        params: dict | None = None,
        protect: list[str] | None = None,
        principal: str | None = None,
    ) -> list[dict]:
        """Run a match clause through the entitlement wrapper.

        ``scope`` names the variables ``match_clause`` produces; every one is
        filtered against the caller. ``final_return`` runs after filtering.
        """
        from . import mediation

        policy = self.config.security
        if not policy.mediated:
            raise RuntimeError("secure_run() requires security.mode: mediated")
        resolved, _ = mediation.resolve_principal(
            policy, principal, self.config.env_snapshot)
        final = mediation.validate_final_return(final_return, scope)
        query = mediation.compose(policy, match_clause, scope, final, protect)
        merged = {**(params or {}), **mediation.security_params(policy, resolved)}
        return self._executor.run(query, merged, read_only=True)


def load_pytools(config: Config, executor: Neo4jExecutor) -> tuple[list[Tool], bool]:
    """Import every ``*.py`` in the bundle's ``pytools/`` and collect their tools.

    Returns ``(tools, used_raw_executor)`` — the flag lets the server warn when a
    mediated bundle's code tools took an unfiltered path.
    """
    directory = config.pytools_dir
    if not directory or not directory.exists():
        return [], False

    ctx = ToolContext(config=config, executor=executor)
    tools: list[Tool] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"bundle_pytool_{config.active_bundle}_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        builder = getattr(module, "build_tools", None)
        if builder is None:
            raise AttributeError(f"{path.name}: pytool module must define build_tools(ctx)")
        result = builder(ctx)
        if result:
            tools.extend(result)
    return tools, ctx.used_raw_executor

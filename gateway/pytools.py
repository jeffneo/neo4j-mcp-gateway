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


@dataclass
class ToolContext:
    """What a pytool module's ``build_tools`` receives."""

    config: Config
    executor: Neo4jExecutor


def load_pytools(config: Config, executor: Neo4jExecutor) -> list[Tool]:
    """Import every ``*.py`` in the bundle's ``pytools/`` and collect their tools."""
    directory = config.pytools_dir
    if not directory or not directory.exists():
        return []

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
    return tools

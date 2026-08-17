"""Use-case *bundles*: swappable packages of tools + data + docs + config.

A bundle is a self-contained folder under ``bundles/`` that turns this generic
gateway into a specific use case (account-takeover, IAM, …):

    bundles/<name>/
      bundle.yaml     # metadata + non-secret config (this module parses it)
      .env            # optional, git-ignored: this bundle's Neo4j credentials
      tools/*.yaml    # the use-case tools
      data/*.cypher   # demo dataset generator(s)
      ...docs...

The gateway loads exactly one *active* bundle (chosen by ``ACTIVE_BUNDLE`` or
``--bundle``). The engine (everything under ``gateway/``) never changes per
use case — only the bundle does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class BundleManifest:
    """Parsed, non-secret configuration from a bundle's ``bundle.yaml``.

    Deliberately holds NO connection details — Neo4j URI/user/password/database
    are credentials/env concerns and live only in ``.env`` files (root, then the
    bundle's git-ignored ``.env`` which overrides). A bundle may therefore point
    at an entirely separate Neo4j instance without any secrets in committed YAML.
    """

    name: str
    description: str = ""
    instructions: str = ""
    read_only: bool | None = None        # optional downstream write-cypher toggle
    hide_tools: list[str] | None = None  # proxied tool names to hide (e.g. read-cypher)
    usecase_prefix: str | None = None    # optional tool-name namespace override
    path: Path | None = None             # the bundle directory


def load_manifest(bundle_dir: Path) -> BundleManifest:
    """Parse ``<bundle_dir>/bundle.yaml`` into a :class:`BundleManifest`."""
    f = bundle_dir / "bundle.yaml"
    if not f.exists():
        raise FileNotFoundError(f"no bundle.yaml in {bundle_dir}")
    raw = yaml.safe_load(f.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{f}: top level must be a mapping")

    downstream = raw.get("downstream") or {}
    if not isinstance(downstream, dict):
        downstream = {}
    read_only = downstream.get("read_only")
    hide_tools = downstream.get("hide") or []
    if not isinstance(hide_tools, list):
        raise ValueError(f"{f}: downstream.hide must be a list of tool names")

    return BundleManifest(
        name=raw.get("name") or bundle_dir.name,
        description=raw.get("description", "") or "",
        instructions=raw.get("instructions", "") or "",
        read_only=read_only,
        hide_tools=[str(t) for t in hide_tools],
        usecase_prefix=raw.get("usecase_prefix"),
        path=bundle_dir,
    )


def list_bundles(bundles_dir: Path) -> list[BundleManifest]:
    """Discover every bundle (a dir with a ``bundle.yaml``) under ``bundles_dir``.

    Directories whose names start with ``_`` (e.g. ``_template``) are skipped.
    """
    if not bundles_dir.exists():
        return []
    found: list[BundleManifest] = []
    for d in sorted(bundles_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("_") and (d / "bundle.yaml").exists():
            try:
                found.append(load_manifest(d))
            except (ValueError, FileNotFoundError):
                # A malformed bundle.yaml shouldn't hide the others from a listing.
                continue
    return found

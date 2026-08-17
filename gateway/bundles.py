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

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODE_OPEN = "open"
MODE_MEDIATED = "mediated"
VALID_MODES = (MODE_OPEN, MODE_MEDIATED)


@dataclass
class SecurityPolicy:
    """How a bundle exposes its data. Declared under ``security:`` in bundle.yaml.

    ``mode`` is REQUIRED — every bundle must state its posture, so "no entitlement
    filtering" is a recorded decision rather than a silent default.

    * ``open``     — tools read the graph directly. Appropriate when every consumer
                     of this bundle is uniformly entitled to all of its data.
    * ``mediated`` — reads are wrapped in an authorization prelude and filtered
                     against the caller's principals before rows are returned.
    """

    mode: str
    permissions_property: str = "Permissions.Read"
    # Labels that are expected to carry ``permissions_property``. Used by
    # scripts/validate_bundle.py as a fail-closed data-quality guard: a record
    # that should be permissioned but isn't would otherwise flow as reference data.
    protected_labels: list[str] = field(default_factory=list)

    @property
    def mediated(self) -> bool:
        return self.mode == MODE_MEDIATED


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
    security: "SecurityPolicy | None" = None  # required in bundle.yaml (see load_manifest)
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

    # security.mode is REQUIRED: every bundle states whether its data is filtered.
    sec_raw = raw.get("security")
    if not isinstance(sec_raw, dict) or not sec_raw.get("mode"):
        raise ValueError(
            f"{f}: 'security.mode' is required and must be one of {list(VALID_MODES)}.\n"
            f"  Add to {bundle_dir.name}/bundle.yaml:\n"
            f"    security:\n"
            f"      mode: open        # or 'mediated' to entitlement-filter every read"
        )
    mode = str(sec_raw["mode"]).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"{f}: security.mode must be one of {list(VALID_MODES)}, got {mode!r}")
    protected_labels = sec_raw.get("protected_labels") or []
    if not isinstance(protected_labels, list):
        raise ValueError(f"{f}: security.protected_labels must be a list of labels")
    security = SecurityPolicy(
        mode=mode,
        permissions_property=str(sec_raw.get("permissions_property") or "Permissions.Read"),
        protected_labels=[str(x) for x in protected_labels],
    )

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
        security=security,
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

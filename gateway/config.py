"""Environment-based configuration for the Neo4j MCP gateway.

Settings come from environment variables (loaded from ``.env`` files via
``python-dotenv``) plus the active *bundle*'s ``bundle.yaml``.

A **bundle** (see :mod:`gateway.bundles`) is a swappable folder under
``bundles/`` that supplies the tools, demo data, docs, and non-secret config for
one use case. ``ACTIVE_BUNDLE`` (or ``--bundle``) selects it.

Resolution order:
  1. root ``.env``            — shared defaults / credentials
  2. bundle ``.env``          — per-bundle overrides (wins over root)
  3. ``bundle.yaml``          — declares database / instructions / prefix / read-only

The same Neo4j credentials feed *both* the official downstream MCP server and
the YAML tool executor (the ``neo4j`` Python driver).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .bundles import BundleManifest, list_bundles, load_manifest

# Project root = the directory that contains the ``gateway/`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLES_DIR = PROJECT_ROOT / "bundles"
DEFAULT_BUNDLE = "ato"


def _load_root_env() -> None:
    """Load the repo-root ``.env`` (shared defaults) if present."""
    root_env = PROJECT_ROOT / ".env"
    if root_env.exists():
        load_dotenv(root_env)
    else:
        load_dotenv()  # fall back to a cwd-upward search


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved gateway configuration for the active bundle."""

    # --- Active bundle ---
    active_bundle: str
    bundle: BundleManifest

    # --- Neo4j connection (shared by downstream server and YAML executor) ---
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str

    # --- Official downstream Neo4j MCP server launch ---
    downstream_cmd: str
    downstream_extra_env: dict[str, str] = field(default_factory=dict)

    # --- YAML use-case tools ---
    tools_dir: Path = BUNDLES_DIR / DEFAULT_BUNDLE / "tools"
    data_dir: Path = BUNDLES_DIR / DEFAULT_BUNDLE / "data"
    usecase_prefix: str = "usecase_"

    # --- Server identity (model-facing) ---
    server_name: str = "neo4j-mcp-gateway"
    instructions: str = ""

    @property
    def downstream_argv(self) -> list[str]:
        argv = shlex.split(self.downstream_cmd)
        if not argv:
            raise ValueError("NEO4J_MCP_CMD resolved to an empty command")
        return argv

    def downstream_env(self) -> dict[str, str]:
        """Full environment for the downstream child (current env + our overrides)."""
        env = dict(os.environ)
        env.update(
            {
                "NEO4J_URI": self.neo4j_uri,
                "NEO4J_USERNAME": self.neo4j_username,
                "NEO4J_PASSWORD": self.neo4j_password,
                "NEO4J_DATABASE": self.neo4j_database,
            }
        )
        env.update(self.downstream_extra_env)
        return env

    @classmethod
    def from_env(cls, active_bundle: str | None = None) -> "Config":
        """Build a :class:`Config` for the active bundle.

        ``active_bundle`` (or ``$ACTIVE_BUNDLE``) names a folder under
        ``bundles/``. Loads the root ``.env`` then the bundle's ``.env`` (which
        overrides), then reads ``bundle.yaml`` for non-secret declarations.
        """
        _load_root_env()

        active = active_bundle or os.getenv("ACTIVE_BUNDLE") or DEFAULT_BUNDLE
        bundle_dir = BUNDLES_DIR / active
        if not (bundle_dir / "bundle.yaml").exists():
            available = ", ".join(b.name for b in list_bundles(BUNDLES_DIR)) or "(none)"
            raise SystemExit(
                f"[gateway] ACTIVE_BUNDLE={active!r} not found under {BUNDLES_DIR}.\n"
                f"           Available bundles: {available}"
            )

        # Per-bundle .env overrides the root .env (credentials, or NEO4J_MCP_CMD, ...).
        bundle_env = bundle_dir / ".env"
        if bundle_env.exists():
            load_dotenv(bundle_env, override=True)

        manifest = load_manifest(bundle_dir)

        # Downstream passthroughs. read_only precedence: env wins, else bundle.yaml.
        extra_env: dict[str, str] = {"NEO4J_TELEMETRY": _env("NEO4J_TELEMETRY", "false")}
        read_only = _env_bool("NEO4J_READ_ONLY")
        if read_only is None:
            read_only = manifest.read_only
        if read_only is not None:
            extra_env["NEO4J_READ_ONLY"] = "true" if read_only else "false"
        for name in ("NEO4J_LOG_LEVEL", "NEO4J_LOG_FORMAT", "NEO4J_SCHEMA_SAMPLE_SIZE"):
            val = os.getenv(name)
            if val:
                extra_env[name] = val

        # Database precedence: bundle.yaml declaration wins, else env, else 'neo4j'.
        database = manifest.database or _env("NEO4J_DATABASE", "neo4j")

        # Tools default to the bundle's tools/, but TOOLS_DIR can override (handy
        # for pointing the dev loop at a reference/answers folder).
        tools_override = os.getenv("TOOLS_DIR")
        tools_dir = Path(tools_override).expanduser() if tools_override else bundle_dir / "tools"

        default_instructions = (
            f"Neo4j gateway for the '{active}' use case. Generic tools "
            "(get-schema, read-cypher, write-cypher, list-gds-procedures) are proxied "
            "from the official Neo4j MCP server; curated use-case tools are prefixed."
        )

        return cls(
            active_bundle=active,
            bundle=manifest,
            neo4j_uri=_env("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_username=_env("NEO4J_USERNAME", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD", "password"),
            neo4j_database=database,
            downstream_cmd=_env("NEO4J_MCP_CMD", "uvx neo4j-mcp-server"),
            downstream_extra_env=extra_env,
            tools_dir=tools_dir,
            data_dir=bundle_dir / "data",
            usecase_prefix=os.getenv("USECASE_PREFIX") or manifest.usecase_prefix or "usecase_",
            server_name=os.getenv("GATEWAY_NAME") or manifest.name or "neo4j-mcp-gateway",
            instructions=manifest.instructions or default_instructions,
        )

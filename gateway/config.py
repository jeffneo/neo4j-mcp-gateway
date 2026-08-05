"""Environment-based configuration for the Neo4j MCP gateway.

All settings come from environment variables, loaded from a local ``.env`` file
via ``python-dotenv`` when present. Nothing here is secret at rest: the ``.env``
file (git-ignored) holds the real credentials; ``.env.example`` documents them.

The same Neo4j credentials feed *both*:
  1. the official downstream Neo4j MCP server (passed through as its env vars), and
  2. the YAML use-case tool executor (via the ``neo4j`` Python driver).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory that contains the ``gateway/`` package.
# Used to resolve default paths (e.g. ``tools/``) independent of the caller's cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Load ``.env`` from the project root (and fall back to cwd-upward search)."""
    root_env = PROJECT_ROOT / ".env"
    if root_env.exists():
        load_dotenv(root_env)
    else:
        # Also honour a .env discovered from the current working directory.
        load_dotenv()


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved gateway configuration."""

    # --- Neo4j connection (shared by downstream server and YAML executor) ---
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str

    # --- Official downstream Neo4j MCP server launch ---
    # A full command line, e.g. "uvx neo4j-mcp-server" or
    # "docker run -i --rm ... neo4j/mcp". Split shell-style into argv.
    downstream_cmd: str
    # Extra env vars to hand to the downstream server (in addition to the four
    # NEO4J_* credentials above), e.g. NEO4J_READ_ONLY / NEO4J_TELEMETRY.
    downstream_extra_env: dict[str, str] = field(default_factory=dict)

    # --- YAML use-case tools ---
    tools_dir: Path = PROJECT_ROOT / "tools"
    # Prefix applied to every YAML tool name so it can never collide with the
    # official proxied tools (get-schema, read-cypher, write-cypher, ...).
    usecase_prefix: str = "usecase_"

    # --- Server identity ---
    server_name: str = "neo4j-mcp-gateway"

    @property
    def downstream_argv(self) -> list[str]:
        """The downstream command split into an argv list for spawning over stdio."""
        argv = shlex.split(self.downstream_cmd)
        if not argv:
            raise ValueError("NEO4J_MCP_CMD resolved to an empty command")
        return argv

    def downstream_env(self) -> dict[str, str]:
        """Full environment for the downstream process.

        We start from the current process environment (so PATH, HOME, etc. are
        available for ``uvx`` / ``docker`` / a built binary) and layer the Neo4j
        credentials plus any explicit passthroughs on top.
        """
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
    def from_env(cls) -> "Config":
        """Build a :class:`Config` from environment variables (loading ``.env``)."""
        _load_env()

        # Optional downstream passthroughs — only forwarded if explicitly set,
        # except telemetry which we default to "false" for a quiet local dev loop.
        extra_env: dict[str, str] = {"NEO4J_TELEMETRY": _env("NEO4J_TELEMETRY", "false")}
        for name in ("NEO4J_READ_ONLY", "NEO4J_LOG_LEVEL", "NEO4J_LOG_FORMAT", "NEO4J_SCHEMA_SAMPLE_SIZE"):
            val = os.getenv(name)
            if val is not None and val != "":
                extra_env[name] = val

        tools_dir = os.getenv("TOOLS_DIR")
        resolved_tools_dir = Path(tools_dir).expanduser() if tools_dir else PROJECT_ROOT / "tools"

        return cls(
            neo4j_uri=_env("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_username=_env("NEO4J_USERNAME", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD", "password"),
            neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
            downstream_cmd=_env("NEO4J_MCP_CMD", "uvx neo4j-mcp-server"),
            downstream_extra_env=extra_env,
            tools_dir=resolved_tools_dir,
            usecase_prefix=_env("USECASE_PREFIX", "usecase_"),
            server_name=_env("GATEWAY_NAME", "neo4j-mcp-gateway"),
        )

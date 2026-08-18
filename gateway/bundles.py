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

import re

import yaml

GRANT_PROPERTY = "property"
GRANT_PATH = "path"
GRANT_BOTH = "both"
VALID_GRANT_MODELS = (GRANT_PROPERTY, GRANT_PATH, GRANT_BOTH)

MODE_OPEN = "open"
MODE_MEDIATED = "mediated"
VALID_MODES = (MODE_OPEN, MODE_MEDIATED)


@dataclass
class Grant:
    """A rule saying a caller may read a row, expressed as a path to it.

    ``via`` is a bare Cypher pattern binding two names the engine supplies:
    ``caller`` (the authenticated principal node) and ``resource`` (the row being
    tested). ``reason`` is human-readable and is what an access explanation
    quotes, so write it as the policy statement it represents.

    A grant is a statement *about a row*, so the pattern must bind ``resource``.
    Role-based entitlements that hold regardless of the row ("supervision reads
    everything") are not paths — keep those in the property model and run
    ``grant_model: both``.
    """

    label: str
    via: str
    reason: str = ""


@dataclass
class IdentityConfig:
    """How to turn a principal string into the set of principals it holds.

    Every field is a graph-shape detail, so a bundle whose identity model uses
    different labels/relationships/properties can be mediated without code.
    """

    labels: list[str] = field(default_factory=lambda: ["User", "Principal"])
    match_keys: list[str] = field(
        default_factory=lambda: ["username", "email", "mail", "userPrincipalName", "upn", "name", "id"]
    )
    group_rels: list[str] = field(
        default_factory=lambda: ["MEMBER_OF", "MEMBER_OF_GROUP", "IN_GROUP", "HAS_GROUP"]
    )
    group_name_keys: list[str] = field(
        default_factory=lambda: ["name", "group", "displayName", "email", "mail", "id"]
    )
    inline_group_list: str = "AdGroupList"


@dataclass
class PrincipalConfig:
    """Where the caller's identity comes from, and whether it may be overridden."""

    env: list[str] = field(
        default_factory=lambda: ["NEO4J_MCP_PRINCIPAL", "NEO4J_MCP_AUTH_SUBJECT", "USER_EMAIL"]
    )
    everyone: str = "everyone"
    allow_impersonation: bool = False


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
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    principal: PrincipalConfig = field(default_factory=PrincipalConfig)
    # Register the open-ended text2cypher tool (secure-read-cypher). Turn off to
    # expose ONLY curated mediated tools — the tightest posture.
    # How a row's readability is decided:
    #   property — a list-valued ACL on the row (materialised upstream)
    #   path     — a path from the caller to the row (derived at query time)
    #   both     — either suffices. Not only a migration step: role-based rules
    #              are naturally ACLs while relationship-based rules are naturally
    #              paths, and most real models contain some of each.
    grant_model: str = GRANT_PROPERTY
    grants: list[Grant] = field(default_factory=list)
    # Label -> the property that identifies one of its rows, e.g. {Trade: tradeId}.
    # Used by explain-access to find the row a question is about.
    resource_keys: dict[str, str] = field(default_factory=dict)
    expose_open_query_tool: bool = True
    # Keep the proxied raw read-cypher despite mediation. Defeats the guarantee;
    # requires an explicit opt-in and is logged loudly.
    allow_unmediated_read: bool = False

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

    id_raw = sec_raw.get("identity") or {}
    if not isinstance(id_raw, dict):
        raise ValueError(f"{f}: security.identity must be a mapping")
    defaults = IdentityConfig()
    identity = IdentityConfig(
        labels=[str(x) for x in (id_raw.get("labels") or defaults.labels)],
        match_keys=[str(x) for x in (id_raw.get("match_keys") or defaults.match_keys)],
        group_rels=[str(x) for x in (id_raw.get("group_rels") or defaults.group_rels)],
        group_name_keys=[str(x) for x in (id_raw.get("group_name_keys") or defaults.group_name_keys)],
        inline_group_list=str(id_raw.get("inline_group_list") or defaults.inline_group_list),
    )
    # Relationship types are interpolated into the Cypher pattern (they cannot be
    # parameterised), so they must be safe identifiers.
    for rel in identity.group_rels:
        if not rel.replace("_", "").isalnum():
            raise ValueError(f"{f}: security.identity.group_rels contains invalid type {rel!r}")
    if not identity.labels:
        raise ValueError(f"{f}: security.identity.labels must name at least one label")
    for label in identity.labels:
        if not label.replace("_", "").isalnum():
            raise ValueError(f"{f}: security.identity.labels contains invalid label {label!r}")

    p_raw = sec_raw.get("principal") or {}
    if not isinstance(p_raw, dict):
        raise ValueError(f"{f}: security.principal must be a mapping")
    p_defaults = PrincipalConfig()
    principal = PrincipalConfig(
        env=[str(x) for x in (p_raw.get("env") or p_defaults.env)],
        everyone=str(p_raw.get("everyone") or p_defaults.everyone),
        allow_impersonation=bool(p_raw.get("allow_impersonation", False)),
    )

    grant_model = str(sec_raw.get("grant_model") or GRANT_PROPERTY).strip().lower()
    if grant_model not in VALID_GRANT_MODELS:
        raise ValueError(
            f"{f}: security.grant_model must be one of {list(VALID_GRANT_MODELS)}, got {grant_model!r}")
    raw_grants = sec_raw.get("grants") or []
    if not isinstance(raw_grants, list):
        raise ValueError(f"{f}: security.grants must be a list")
    grants: list[Grant] = []
    for i, g in enumerate(raw_grants):
        if not isinstance(g, dict) or not g.get("label") or not g.get("via"):
            raise ValueError(f"{f}: security.grants[{i}] needs 'label' and 'via'")
        label = str(g["label"])
        if not label.replace("_", "").isalnum():
            raise ValueError(f"{f}: security.grants[{i}] has invalid label {label!r}")
        via = str(g["via"])
        # The pattern is interpolated into Cypher, so it must be a bare MATCH
        # pattern binding both names the engine supplies.
        if "caller" not in via:
            raise ValueError(f"{f}: security.grants[{i}] must start from (caller)")
        if not re.search(r"\bresource\b", via):
            raise ValueError(
                f"{f}: security.grants[{i}] must bind (resource) — a grant is a statement "
                "about a row. Role-based rules that ignore the row belong in the property "
                "model; use grant_model: both.")
        if re.search(r"\b(return|create|merge|delete|set|remove|drop|detach|call|union|with)\b|;",
                     via.lower()):
            raise ValueError(f"{f}: security.grants[{i}] must be a bare MATCH pattern")
        grants.append(Grant(label=label, via=via, reason=str(g.get("reason") or "")))
    if grant_model in (GRANT_PATH, GRANT_BOTH) and not grants:
        raise ValueError(f"{f}: security.grant_model={grant_model!r} requires at least one grant")

    raw_keys = sec_raw.get("resource_keys") or {}
    if not isinstance(raw_keys, dict):
        raise ValueError(f"{f}: security.resource_keys must be a mapping of label -> property")
    resource_keys = {str(k): str(v) for k, v in raw_keys.items()}
    for label, prop in resource_keys.items():
        if not label.replace("_", "").isalnum() or not prop.replace("_", "").isalnum():
            raise ValueError(f"{f}: security.resource_keys has invalid entry {label!r}: {prop!r}")

    security = SecurityPolicy(
        mode=mode,
        grant_model=grant_model,
        grants=grants,
        resource_keys=resource_keys,
        permissions_property=str(sec_raw.get("permissions_property") or "Permissions.Read"),
        protected_labels=[str(x) for x in protected_labels],
        identity=identity,
        principal=principal,
        expose_open_query_tool=bool(sec_raw.get("expose_open_query_tool", True)),
        allow_unmediated_read=bool(sec_raw.get("allow_unmediated_read", False)),
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

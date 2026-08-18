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

# Where the caller's principals are resolved. Only SOURCE_GRAPH puts the caller
# node in reach of the data query; the other two are "separated" (see
# IdentityConfig and SEPARATION_TRADEOFFS in gateway/identity_sources.py).
SOURCE_GRAPH = "graph"
SOURCE_COMPOSITE = "composite"
SOURCE_REMOTE = "remote"
VALID_IDENTITY_SOURCES = (SOURCE_GRAPH, SOURCE_COMPOSITE, SOURCE_REMOTE)
SEPARATED_SOURCES = (SOURCE_COMPOSITE, SOURCE_REMOTE)

# A composite constituent is interpolated into the Cypher `USE` clause, so it is
# validated strictly rather than quoted.
_GRAPH_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)*$")
# An ACL property key is interpolated into Cypher inside backticks.
_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


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

    ``source`` decides WHERE that resolution happens, which is the one setting
    that changes the architecture rather than the graph shape:

    * ``graph``     — the identity graph lives in the same database as the data.
                      The prelude traverses it in the same statement, and the
                      caller node is available to path grants and anchors.
    * ``composite`` — identity and data are separate databases joined by a
                      composite database. One statement still, via ``USE``.
    * ``remote``    — identity is resolved out of band (a second Neo4j
                      connection) and the resulting principals are passed into
                      the data query as a parameter. Two round trips.

    The two separated sources keep path grants and anchoring — both are
    traversals from the caller, and both are cut at a proxy node present in each
    database. What they need instead is those proxies. See SEPARATION_TRADEOFFS
    in gateway/identity_sources.py and GRANT_SPLITTING in gateway/mediation.py.
    """

    source: str = "graph"
    # source: composite — the constituent aliases, e.g. "fed.identity"/"fed.data".
    identity_graph: str = ""
    data_graph: str = ""
    # source: remote — env prefix for the identity connection, so the credentials
    # stay in a git-ignored .env like every other connection in this repo.
    remote_env_prefix: str = "IDENTITY"
    # Label -> property holding the identity-side name at the boundary, e.g.
    # {Client: coverageTeam}. Declaring one lets a grant cut at that PROPERTY
    # instead of at a proxy node, which removes the proxies entirely and drops a
    # hop. See PROPERTY_CUT in gateway/mediation.py — including the hazard that
    # the property and the relationship are two recordings of the same fact.
    boundary_properties: dict[str, str] = field(default_factory=dict)

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
    # Refuse to start unless an audit log is configured. Fail-closed, like `mode`.
    require_audit: bool = False
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
        source=str(id_raw.get("source") or defaults.source).strip().lower(),
        identity_graph=str(id_raw.get("identity_graph") or "").strip(),
        data_graph=str(id_raw.get("data_graph") or "").strip(),
        remote_env_prefix=str(id_raw.get("remote_env_prefix") or defaults.remote_env_prefix).strip(),
        boundary_properties={str(k): str(v)
                             for k, v in (id_raw.get("boundary_properties") or {}).items()},
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

    if identity.source not in VALID_IDENTITY_SOURCES:
        raise ValueError(
            f"{f}: security.identity.source must be one of {list(VALID_IDENTITY_SOURCES)}, "
            f"got {identity.source!r}")
    if identity.source == SOURCE_COMPOSITE:
        for fieldname in ("identity_graph", "data_graph"):
            value = getattr(identity, fieldname)
            if not value:
                raise ValueError(
                    f"{f}: security.identity.source=composite requires "
                    f"security.identity.{fieldname} (the constituent alias, e.g. 'fed.identity')")
            if not _GRAPH_REF.match(value):
                raise ValueError(
                    f"{f}: security.identity.{fieldname}={value!r} is not a valid graph reference")
        if identity.identity_graph == identity.data_graph:
            raise ValueError(
                f"{f}: security.identity.identity_graph and data_graph are the same constituent "
                f"({identity.data_graph!r}); use source: graph instead")
    for label, prop in identity.boundary_properties.items():
        # Both are interpolated into Cypher (the label into a pattern, the
        # property inside backticks), so neither may break out.
        if not label.replace("_", "").isalnum() or not _PROPERTY_KEY.match(prop):
            raise ValueError(
                f"{f}: security.identity.boundary_properties has invalid entry "
                f"{label!r}: {prop!r}")
    if identity.source == SOURCE_REMOTE and not identity.remote_env_prefix.replace("_", "").isalnum():
        raise ValueError(
            f"{f}: security.identity.remote_env_prefix={identity.remote_env_prefix!r} is not a "
            "valid environment-variable prefix")

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

    # A separated identity source puts the caller node out of reach of the data
    # query — composite databases refuse to import entity values across a USE
    # boundary, and a remote source has no caller node in this database at all.
    # A path grant is a traversal FROM the caller, so it cannot be evaluated.
    # Fail at load rather than silently degrading to "no grant matched", which
    # under grant_model=path would deny everything and under `both` would quietly
    # fall back to ACLs without saying so.
    if identity.source in SEPARATED_SOURCES and grants:
        # A separated source keeps path grants, because a split grant needs a
        # VALUE from the caller rather than the caller node: the data-side half is
        # re-rooted at a proxy and matched against authzPrincipals / principalId.
        # Under `composite` that value is bound in-statement; under `remote` it
        # arrives as a parameter. Neither needs a caller node in the data database.
        #
        # Each grant must still be cuttable at the identity/data boundary.
        # Checking at load makes an uncuttable pattern a startup error instead of
        # a silent denial at query time — see GRANT_SPLITTING in mediation.py.
        from . import mediation
        probe = SecurityPolicy(mode=mode, identity=identity, grant_model=grant_model)
        for i, grant in enumerate(grants):
            try:
                mediation.split_grant(probe, grant.via)
            except mediation.GrantSplitError as exc:
                raise ValueError(
                    f"{f}: security.grants[{i}] ({grant.label}) cannot be evaluated with "
                    f"security.identity.source={identity.source!r}: {exc}") from exc

    # Interpolated into Cypher as a backtick-quoted property key (see
    # COMPOSITE_PROPERTY_ACCESS in gateway/mediation.py), so it must not be able
    # to break out of the quoting.
    permissions_property = str(sec_raw.get("permissions_property") or "Permissions.Read")
    if not _PROPERTY_KEY.match(permissions_property):
        raise ValueError(
            f"{f}: security.permissions_property={permissions_property!r} must be letters, "
            "digits, underscores and dots only")

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
        permissions_property=permissions_property,
        protected_labels=[str(x) for x in protected_labels],
        identity=identity,
        principal=principal,
        expose_open_query_tool=bool(sec_raw.get("expose_open_query_tool", True)),
        require_audit=bool(sec_raw.get("require_audit", False)),
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

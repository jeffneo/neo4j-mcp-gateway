"""Audit logging — a record of authorization decisions, not of data.

Every tool call through the gateway appends one JSON object to an append-only
log: who called, as whom, which tool, in which bundle, how the caller was
resolved, whether it succeeded, and how many rows survived the entitlement
filter.

WHAT IS DELIBERATELY NOT LOGGED
-------------------------------
**Row contents, ever.** An audit log that copies the rows it audits is a second,
less-protected replica of exactly the data the entitlement filter exists to
restrict — usually on a filesystem with weaker controls than the database, often
shipped to a log aggregator that a different team can read. The log records the
COUNT of rows returned and nothing about them.

Tool arguments are the judgement call. "Which client did they ask about" is
genuinely useful to an auditor and is also, itself, potentially sensitive. The
default records argument *names* only; set ``NEO4J_MCP_AUDIT_ARGUMENTS=true`` to
record values, which is a deliberate decision a deployment makes with its own
data-classification rules in hand.

WHAT MATTERS MOST IN THE RECORD
-------------------------------
``impersonated`` — a call running as someone other than the connected user is a
privileged action, and it is the first thing a reviewer looks for. It is a
top-level boolean rather than something to infer from ``principalSource``.

CONFIGURATION — env only, like connections
------------------------------------------
    NEO4J_MCP_AUDIT_LOG=/var/log/neo4j-mcp/audit.jsonl   enables logging
    NEO4J_MCP_AUDIT_ARGUMENTS=true                       also record arg values

A bundle may declare ``security.require_audit: true``, and the gateway then
refuses to start unless a log path is configured — the same fail-closed stance
as ``security.mode``: running unaudited becomes a recorded decision rather than
something that happens by omission.

Never stdout: that carries the MCP stdio protocol framing.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from fastmcp.server.middleware import Middleware, MiddlewareContext

ENV_PATH = "NEO4J_MCP_AUDIT_LOG"
ENV_ARGUMENTS = "NEO4J_MCP_AUDIT_ARGUMENTS"


def audit_path(env: Mapping[str, str] | None = None) -> str:
    return str((os.environ if env is None else env).get(ENV_PATH, "")).strip()


def include_arguments(env: Mapping[str, str] | None = None) -> bool:
    value = str((os.environ if env is None else env).get(ENV_ARGUMENTS, "")).strip().lower()
    return value in {"true", "1", "yes"}


class AuditSink:
    """Append-only JSON Lines writer.

    One object per line, flushed per record: a crash must not lose the tail of
    the trail, and partial lines are worse than missing ones. A lock keeps
    concurrent tool calls from interleaving mid-line.
    """

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError as exc:
            # A failed write must be loud but must not take down the tool call:
            # losing the gateway is a bigger operational event than losing a
            # line. Deployments that cannot tolerate a gap should ship the log
            # from a filesystem they monitor.
            print(f"[gateway] AUDIT WRITE FAILED: {exc}", file=sys.stderr, flush=True)


def _row_count(result) -> int | None:
    """Rows returned, taken from the structured payload. Never the rows."""
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        if isinstance(payload.get("count"), int):
            return payload["count"]
        records = payload.get("records") or payload.get("result")
        if isinstance(records, list):
            return len(records)
    return None


class AuditMiddleware(Middleware):
    """Record one line per tool call.

    Wraps every tool the gateway exposes, including the proxied official ones —
    so an `open` bundle's raw ``read-cypher`` is audited on the same terms as a
    mediated curated tool. That matters: the unfiltered path is the one a
    reviewer most wants a trail for.
    """

    def __init__(self, sink: AuditSink, configs, log_arguments: bool = False):
        self._sink = sink
        self._log_arguments = log_arguments
        # Longest prefix first, so 'client_platform_split_' wins over
        # 'client_platform_' when both bundles are mounted.
        self._by_prefix = sorted(
            ((f"{c.active_bundle}_", c) for c in configs),
            key=lambda pair: len(pair[0]), reverse=True)
        self._only = configs[0] if len(configs) == 1 else None

    def _config_for(self, tool: str):
        if self._only is not None:
            return self._only, tool
        for prefix, config in self._by_prefix:
            if tool.startswith(prefix):
                return config, tool[len(prefix):]
        return None, tool

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool = getattr(context.message, "name", "") or ""
        arguments = dict(getattr(context.message, "arguments", None) or {})
        config, bare = self._config_for(tool)

        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": "tool_call",
            "tool": bare,
            "bundle": config.active_bundle if config else None,
        }

        if config is not None:
            policy = config.security
            record["mode"] = policy.mode
            if policy.mediated:
                record["identitySource"] = policy.identity.source
                record["grantModel"] = policy.grant_model
            requested = str(arguments.get("principal") or "").strip()
            try:
                from . import mediation
                principal, source = mediation.resolve_principal(
                    policy, requested or None, config.env_snapshot)
                record["principal"] = principal
                record["principalSource"] = source
            except Exception:
                # Never let audit bookkeeping decide whether a call runs.
                record["principal"] = None
                record["principalSource"] = "unresolved"
            record["impersonated"] = bool(requested)

        # Argument NAMES always: they say which question was asked without
        # carrying its subject. Values only on explicit opt-in.
        safe = {k: v for k, v in arguments.items() if k != "principal"}
        record["argumentNames"] = sorted(safe)
        if self._log_arguments:
            record["arguments"] = safe

        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            record["outcome"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["durationMs"] = round((time.perf_counter() - start) * 1000, 2)
            self._sink.write(record)
            raise
        record["durationMs"] = round((time.perf_counter() - start) * 1000, 2)
        record["outcome"] = "error" if getattr(result, "is_error", False) else "ok"
        rows = _row_count(result)
        if rows is not None:
            record["rows"] = rows
        self._sink.write(record)
        return result

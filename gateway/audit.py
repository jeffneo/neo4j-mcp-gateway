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
    NEO4J_MCP_AUDIT_FORWARDER=stderr | file:<path> | <your scheme>
                                                         where chain checkpoints go
    NEO4J_MCP_AUDIT_CHECKPOINT_EVERY=100                 records per checkpoint

Records are hash-chained; verify with scripts/verify_audit.py. See
TAMPER_EVIDENCE below for exactly what that does and does not prove.

A bundle may declare ``security.require_audit: true``, and the gateway then
refuses to start unless a log path is configured — the same fail-closed stance
as ``security.mode``: running unaudited becomes a recorded decision rather than
something that happens by omission.

Never stdout: that carries the MCP stdio protocol framing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fastmcp.server.middleware import Middleware, MiddlewareContext

ENV_PATH = "NEO4J_MCP_AUDIT_LOG"
ENV_ARGUMENTS = "NEO4J_MCP_AUDIT_ARGUMENTS"


ENV_FORWARDER = "NEO4J_MCP_AUDIT_FORWARDER"
ENV_CHECKPOINT_EVERY = "NEO4J_MCP_AUDIT_CHECKPOINT_EVERY"


# THE FORWARDER SEAM
# ------------------
# A hash chain only proves tampering if the head hash exists somewhere its writer
# cannot rewrite. This is that seam. Two forwarders ship, neither of them a real
# integration:
#
#   stderr        prints the checkpoint. Enough to SEE the mechanism working and
#                 to pipe somewhere from the supervisor. The dev default.
#   file:<path>   appends checkpoints to a second file. Useful for testing the
#                 verifier; NOT an anchor if it sits on the same host, which is
#                 the whole point — say so rather than let it look like one.
#
# A real deployment registers its own: a SIEM client, a WORM bucket write, an
# append-only table in another database, a signing service. The contract is one
# method, so the integration is small and belongs to whoever owns the SIEM.
#
#     from gateway.audit import register_forwarder
#     class SplunkHEC:
#         def send(self, checkpoint: dict) -> None: ...
#     register_forwarder("splunk", lambda spec: SplunkHEC())
class AuditForwarder(Protocol):
    def send(self, checkpoint: dict) -> None: ...


class StderrForwarder:
    """Prints checkpoints. Visible, unauthenticated, not an anchor."""

    def send(self, checkpoint: dict) -> None:
        print(f"[gateway] AUDIT CHECKPOINT {_canonical(checkpoint)}",
              file=sys.stderr, flush=True)


class FileForwarder:
    """Appends checkpoints to a second file.

    On the same host this anchors nothing — an attacker who can rewrite the log
    can rewrite this too. It exists to exercise the mechanism and to give a
    deployment something to ship off-box.
    """

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, checkpoint: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(checkpoint) + "\n")
            handle.flush()


_FORWARDERS: dict = {
    "stderr": lambda spec: StderrForwarder(),
    "file": lambda spec: FileForwarder(spec),
    "none": lambda spec: None,
}


def register_forwarder(scheme: str, factory) -> None:
    """Register a checkpoint destination, selected by ``<scheme>`` or ``<scheme>:<spec>``."""
    _FORWARDERS[scheme] = factory


def build_forwarder(env: Mapping[str, str] | None = None):
    raw = str((os.environ if env is None else env).get(ENV_FORWARDER, "")).strip()
    if not raw:
        return None
    scheme, _, spec = raw.partition(":")
    factory = _FORWARDERS.get(scheme.strip().lower())
    if factory is None:
        raise SystemExit(
            f"[gateway] unknown {ENV_FORWARDER} {raw!r}. Known: "
            f"{', '.join(sorted(_FORWARDERS))}. Register your own with "
            "gateway.audit.register_forwarder().")
    return factory(spec.strip())


def checkpoint_every(env: Mapping[str, str] | None = None) -> int:
    raw = str((os.environ if env is None else env).get(ENV_CHECKPOINT_EVERY, "")).strip()
    try:
        return max(0, int(raw)) if raw else 100
    except ValueError:
        return 100


def audit_path(env: Mapping[str, str] | None = None) -> str:
    return str((os.environ if env is None else env).get(ENV_PATH, "")).strip()


def include_arguments(env: Mapping[str, str] | None = None) -> bool:
    value = str((os.environ if env is None else env).get(ENV_ARGUMENTS, "")).strip().lower()
    return value in {"true", "1", "yes"}


# TAMPER_EVIDENCE
# ---------------
# Each record carries `seq`, `prev` (the previous record's hash) and its own
# `hash` over the canonical serialisation of everything else. Editing, deleting
# or reordering any line breaks every hash after it, and
# scripts/verify_audit.py names the first line that fails.
#
# BE PRECISE ABOUT WHAT THIS BUYS, because "immutable audit log" is a phrase that
# gets used loosely and a reviewer will press on it:
#
#   detected      a line edited in place; a line removed from the middle; lines
#                 reordered; a line inserted.
#   NOT detected  truncation of the whole file and a fresh start. The new chain
#                 is internally valid — genesis looks exactly like a file that
#                 was never written. Nothing INSIDE the file can close this.
#   NOT prevented anything. An attacker with write access and the code can
#                 recompute the whole chain from the point of change forward.
#
# Both gaps close the same way and only the same way: get the head hash OUT of
# reach of whoever can write the file. That is what a forwarder is for — a
# periodic checkpoint of (seq, hash) to a SIEM, a WORM bucket, or anything
# append-only the gateway host cannot rewrite. The chain makes tampering
# detectable *given* an external anchor; the anchor is not optional decoration.
_GENESIS = "0" * 64


def _canonical(record: dict) -> str:
    """Deterministic serialisation. Key order must not depend on insertion order."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False)


def chain_hash(record: dict, prev: str) -> str:
    """The hash a record must carry, given its predecessor's."""
    body = {k: v for k, v in record.items() if k != "hash"}
    body["prev"] = prev
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class AuditSink:
    """Append-only, hash-chained JSON Lines writer.

    One object per line, flushed per record: a crash must not lose the tail of
    the trail, and partial lines are worse than missing ones. A lock keeps
    concurrent tool calls from interleaving mid-line and keeps the chain linear.
    """

    def __init__(self, path: str, forwarder=None, checkpoint_every: int = 100):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._forwarder = forwarder
        self._checkpoint_every = max(0, int(checkpoint_every))
        # Resume the chain across restarts, or a restart would silently start a
        # second chain and every later verification would report a break.
        self._seq, self._head = self._resume()
        self._anchored = -1        # last seq already checkpointed

    def _resume(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, _GENESIS
        last = None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        last = line
        except OSError:
            return 0, _GENESIS
        if not last:
            return 0, _GENESIS
        try:
            record = json.loads(last)
            return int(record.get("seq", 0)), str(record.get("hash") or _GENESIS)
        except (ValueError, TypeError):
            # An unreadable tail means the chain cannot be continued honestly.
            # Say so loudly rather than starting a fresh one that looks valid.
            print("[gateway] AUDIT: last line is unreadable — the hash chain cannot be "
                  "continued from it. Verification will report a break here.",
                  file=sys.stderr, flush=True)
            return 0, _GENESIS

    def write(self, record: dict) -> None:
        try:
            with self._lock:
                self._seq += 1
                record["seq"] = self._seq
                record["prev"] = self._head
                record["hash"] = chain_hash(record, self._head)
                self._head = record["hash"]
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
                    handle.flush()
                due = (self._checkpoint_every
                       and self._seq % self._checkpoint_every == 0)
                seq, head = self._seq, self._head
        except OSError as exc:
            # A failed write must be loud but must not take down the tool call:
            # losing the gateway is a bigger operational event than losing a
            # line. Deployments that cannot tolerate a gap should ship the log
            # from a filesystem they monitor.
            print(f"[gateway] AUDIT WRITE FAILED: {exc}", file=sys.stderr, flush=True)
            return
        if due and self._forwarder is not None:
            self.checkpoint(seq, head)

    def checkpoint(self, seq: int, head: str) -> None:
        """Publish the chain head somewhere the writer of this file cannot reach."""
        if self._forwarder is None or seq == self._anchored:
            return
        self._anchored = seq
        try:
            self._forwarder.send({
                "event": "audit_checkpoint",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "path": str(self.path), "seq": seq, "head": head,
            })
        except Exception as exc:  # noqa: BLE001 - a forwarder must never fail a call
            print(f"[gateway] AUDIT CHECKPOINT FAILED: {exc}", file=sys.stderr, flush=True)

    def close(self) -> None:
        """Final checkpoint, so a clean shutdown anchors the tail."""
        self.checkpoint(self._seq, self._head)


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

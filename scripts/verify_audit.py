#!/usr/bin/env python
"""Verify the audit log's hash chain, and report the first line that breaks it.

    uv run python scripts/verify_audit.py /var/log/neo4j-mcp/audit.jsonl
    uv run python scripts/verify_audit.py audit.jsonl --checkpoints checkpoints.jsonl

Exits non-zero on any break, so it can gate a compliance job.

WHAT A PASS MEANS, precisely — this is the part worth reading before quoting the
result to anyone:

    A pass proves the file is INTERNALLY consistent: no line was edited,
    removed, reordered or inserted since it was written.

    A pass does NOT prove the file is complete. Delete the whole thing and start
    over and the new chain verifies perfectly, because a genesis record is
    indistinguishable from a log that was simply never written. Nothing inside
    the file can detect that.

That gap closes only from outside, which is what ``--checkpoints`` is for. If the
gateway forwarded chain heads somewhere the log's writer cannot rewrite, this
compares them against the file. A checkpoint whose head does not appear at its
sequence number is proof of rewriting; a checkpoint whose sequence number is past
the end of the file is proof of truncation. Without checkpoints, both are
invisible and this tool says so rather than implying otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.audit import _GENESIS, chain_hash  # noqa: E402


def verify(path: Path) -> tuple[int, list[str], dict[int, str]]:
    """Return (records checked, failures, {seq: hash})."""
    failures: list[str] = []
    heads: dict[int, str] = {}
    prev = _GENESIS
    count = 0
    expected_seq = 0

    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            count += 1
            expected_seq += 1
            try:
                record = json.loads(line)
            except ValueError as exc:
                failures.append(f"line {lineno}: not valid JSON ({exc})")
                break

            seq, stored, claimed_prev = (record.get("seq"), record.get("hash"),
                                         record.get("prev"))
            if stored is None or claimed_prev is None:
                failures.append(
                    f"line {lineno}: record has no hash chain fields — written before "
                    "chaining was enabled, or stripped")
                break
            if seq != expected_seq:
                failures.append(
                    f"line {lineno}: seq is {seq}, expected {expected_seq} — a record was "
                    "inserted or removed")
            if claimed_prev != prev:
                failures.append(
                    f"line {lineno} (seq {seq}): prev does not match the previous record's "
                    "hash — the chain is cut here")
                break
            recomputed = chain_hash(record, prev)
            if recomputed != stored:
                failures.append(
                    f"line {lineno} (seq {seq}): contents do not match the stored hash — "
                    "this record was modified after it was written")
                break
            heads[int(seq)] = stored
            prev = stored
    return count, failures, heads


def check_checkpoints(cp_path: Path, heads: dict[int, str], last_seq: int) -> list[str]:
    failures: list[str] = []
    checked = 0
    with cp_path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                cp = json.loads(line)
            except ValueError:
                failures.append(f"checkpoint line {lineno}: not valid JSON")
                continue
            seq, head = cp.get("seq"), cp.get("head")
            if seq is None or head is None:
                continue
            checked += 1
            if int(seq) > last_seq:
                failures.append(
                    f"checkpoint seq {seq} is past the end of the log (last is {last_seq}) — "
                    f"{int(seq) - last_seq} record(s) are MISSING from the file")
            elif heads.get(int(seq)) != head:
                failures.append(
                    f"checkpoint seq {seq}: anchored head {head[:16]}… does not match the "
                    f"file's {str(heads.get(int(seq)))[:16]}… — the log was rewritten")
    print(f"  {checked} checkpoint(s) compared against the file")
    return failures


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="the audit log (JSON Lines)")
    ap.add_argument("--checkpoints",
                    help="externally anchored chain heads, to detect truncation/rewriting")
    args = ap.parse_args(argv)

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"no such file: {path}")
        return 2

    count, failures, heads = verify(path)
    last_seq = max(heads) if heads else 0
    print(f"audit log: {path}")
    print(f"  {count} record(s), chain verified through seq {last_seq}")

    if args.checkpoints:
        cp = Path(args.checkpoints).expanduser()
        if not cp.exists():
            print(f"  !! no such checkpoint file: {cp}")
            failures.append("checkpoint file missing")
        else:
            failures += check_checkpoints(cp, heads, last_seq)
    else:
        print("  note  no --checkpoints given, so TRUNCATION IS UNDETECTABLE: a log "
              "deleted and restarted verifies clean.")

    if failures:
        print(f"\nFAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nPASSED — internally consistent"
          + (" and consistent with the external anchors" if args.checkpoints else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

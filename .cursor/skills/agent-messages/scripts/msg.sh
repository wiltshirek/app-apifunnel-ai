#!/usr/bin/env python3
"""Agent message bus — append-only log model.

Single source of truth: /tmp/agent-messages/events.log (JSONL, one record per line).
Each record is a message. Line number is its identity (seq).

This replaces the older per-file folder model, which had no causal ordering and
allowed concurrent senders to produce stale-state replies. In the log model:
    - Appends are atomic (fcntl.flock LOCK_EX around write).
    - `seq` is a strictly monotonic line number.
    - Each agent remembers ONE integer — the last seq it has read.
    - `in_reply_to` points to a specific prior seq; if the sender's cursor is
      behind that seq, send is rejected (you can't reply to a message you
      haven't read).
    - `watch` returns every new record since the reader's cursor, not just
      the first. No bursts are missed.

Identity is the basename of $PWD. No config.

Subcommands (stable names; semantics aligned with the log):
    whoami                              print agent identity (workspace basename)
    inbox                               list unread records addressed to me or "all"
    inbox --all                         list every record (debugging)
    read <seq>                          print one record and advance cursor
    peek <seq>                          print without advancing
    mark-seen <seq>                     advance cursor to seq
    send <recipient> <title>            read body from stdin, append record
                                        [--in-reply-to <seq>]  enforce causal link
    watch [interval] [cap]              block until any new record addressed to me,
                                        then print ALL new records in this window
                                        (interval sec poll, cap sec timeout;
                                         defaults: interval=5, cap=1200)
    whois                               unique senders in the log
    clean                               rotate the log if older than 7 days

Legacy .md files in /tmp/agent-messages/ from the prior bus are ignored.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MSG_DIR = Path("/tmp/agent-messages")
LOG_PATH = MSG_DIR / "events.log"
CURSOR_DIR = MSG_DIR / ".cursor"


def identity() -> str:
    """Agent identity = basename of the invoking workspace (PWD)."""
    return os.path.basename(os.getcwd())


def cursor_path(who: str) -> Path:
    return CURSOR_DIR / f"{who}.txt"


def ensure_dirs() -> None:
    MSG_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.touch()


def read_cursor(who: str) -> int:
    p = cursor_path(who)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip() or "0")
    except ValueError:
        return 0


def write_cursor(who: str, seq: int) -> None:
    p = cursor_path(who)
    p.write_text(str(seq))


def iter_records():
    """Yield (seq, record_dict) for every line in the log.

    seq is 1-indexed line number. Lines that fail to parse as JSON are skipped
    with a warning on stderr (no silent loss).
    """
    if not LOG_PATH.exists():
        return
    with LOG_PATH.open("r", encoding="utf-8") as f:
        seq = 0
        for line in f:
            seq += 1
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[msg.sh] skipping malformed record seq={seq}: {exc}",
                      file=sys.stderr)
                continue
            yield seq, rec


def record_at(seq: int):
    for s, rec in iter_records():
        if s == seq:
            return rec
    return None


def addressed_to_me(rec: dict, me: str) -> bool:
    to = rec.get("to")
    return to == me or to == "all"


def fmt_record(seq: int, rec: dict, indent: bool = True) -> str:
    lines = []
    lines.append(f"seq:          {seq}")
    lines.append(f"ts:           {rec.get('ts', '?')}")
    lines.append(f"from:         {rec.get('from', '?')}")
    lines.append(f"to:           {rec.get('to', '?')}")
    lines.append(f"title:        {rec.get('title', '')}")
    if rec.get("in_reply_to"):
        lines.append(f"in_reply_to:  {rec['in_reply_to']}")
    lines.append("---")
    lines.append(rec.get("body", ""))
    return "\n".join(lines)


def append_record(rec: dict) -> int:
    """Atomic append. Returns the seq assigned to this record (1-indexed line number)."""
    ensure_dirs()
    with LOG_PATH.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            existing = sum(1 for _ in f)
            seq = existing + 1
            rec_with_seq = dict(rec)
            rec_with_seq["seq"] = seq
            f.write(json.dumps(rec_with_seq, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return seq


# ----- Commands ------------------------------------------------------------

def cmd_whoami(args):
    ensure_dirs()
    print(identity())


def cmd_inbox(args):
    ensure_dirs()
    show_all = len(args) > 0 and args[0] == "--all"
    me = identity()
    cursor = read_cursor(me)

    unread = []
    read = []
    for seq, rec in iter_records():
        if not show_all and not addressed_to_me(rec, me):
            continue
        if seq > cursor:
            unread.append((seq, rec))
        else:
            read.append((seq, rec))

    header = "# All records" if show_all else f"# Inbox for: {me}"
    print(header)
    print(f"# Log: {LOG_PATH}  (cursor = {cursor})")
    print()

    print("UNREAD:")
    if not unread:
        print("  (none)")
    for seq, rec in unread:
        print(f"  [{seq:>5}] ({rec.get('ts', '?')})  "
              f"{rec.get('from', '?')} → {rec.get('to', '?')}  "
              f"| {rec.get('title', '')}")
    print()

    print("READ:")
    if not read:
        print("  (none)")
    for seq, rec in read:
        print(f"  [{seq:>5}] ({rec.get('ts', '?')})  "
              f"{rec.get('from', '?')} → {rec.get('to', '?')}  "
              f"| {rec.get('title', '')}")
    print()
    print(f"Summary: {len(unread)} unread, {len(read)} read.")


def cmd_read(args):
    ensure_dirs()
    if not args:
        print("usage: msg.sh read <seq>", file=sys.stderr)
        sys.exit(1)
    try:
        seq = int(args[0])
    except ValueError:
        print(f"not a valid seq: {args[0]}", file=sys.stderr)
        sys.exit(1)
    rec = record_at(seq)
    if rec is None:
        print(f"no such record: seq={seq}", file=sys.stderr)
        sys.exit(1)
    print(fmt_record(seq, rec))
    # Advance cursor to max(cursor, seq) — do not rewind.
    me = identity()
    write_cursor(me, max(read_cursor(me), seq))


def cmd_peek(args):
    ensure_dirs()
    if not args:
        print("usage: msg.sh peek <seq>", file=sys.stderr)
        sys.exit(1)
    try:
        seq = int(args[0])
    except ValueError:
        print(f"not a valid seq: {args[0]}", file=sys.stderr)
        sys.exit(1)
    rec = record_at(seq)
    if rec is None:
        print(f"no such record: seq={seq}", file=sys.stderr)
        sys.exit(1)
    print(fmt_record(seq, rec))


def cmd_mark_seen(args):
    ensure_dirs()
    if not args:
        print("usage: msg.sh mark-seen <seq>", file=sys.stderr)
        sys.exit(1)
    try:
        seq = int(args[0])
    except ValueError:
        print(f"not a valid seq: {args[0]}", file=sys.stderr)
        sys.exit(1)
    me = identity()
    write_cursor(me, max(read_cursor(me), seq))
    print(f"cursor advanced: {me} → {read_cursor(me)}")


def cmd_send(args):
    """send <recipient> <title>  (body on stdin)  [--in-reply-to <seq>]"""
    ensure_dirs()
    in_reply_to = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--in-reply-to":
            if i + 1 >= len(args):
                print("--in-reply-to requires a seq", file=sys.stderr)
                sys.exit(1)
            try:
                in_reply_to = int(args[i + 1])
            except ValueError:
                print(f"not a valid seq: {args[i + 1]}", file=sys.stderr)
                sys.exit(1)
            i += 2
        else:
            positional.append(a)
            i += 1

    if len(positional) < 2:
        print("usage: msg.sh send <recipient> <title> [--in-reply-to <seq>]",
              file=sys.stderr)
        sys.exit(1)
    recipient, title = positional[0], positional[1]

    me = identity()

    # Causal guard: can't reply to a record you haven't read.
    # This is not a defensive check on the bus — it's a direct semantic rule:
    # "in-reply-to seq N" asserts the sender has read through at least N. If
    # their cursor is behind N, the assertion is false; reject rather than lie.
    if in_reply_to is not None:
        rec = record_at(in_reply_to)
        if rec is None:
            print(f"in-reply-to refers to missing seq: {in_reply_to}",
                  file=sys.stderr)
            sys.exit(1)
        if read_cursor(me) < in_reply_to:
            print(
                f"cannot send: your cursor is {read_cursor(me)} but "
                f"in-reply-to seq is {in_reply_to}. Read seq {in_reply_to} "
                f"first, then retry.",
                file=sys.stderr,
            )
            sys.exit(2)

    body = sys.stdin.read()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {
        "ts": ts,
        "from": me,
        "to": recipient,
        "title": title,
        "in_reply_to": in_reply_to,
        "body": body,
    }
    seq = append_record(rec)

    # Self-acknowledge: advance own cursor past own send so broadcasts don't
    # show up in our own inbox.
    write_cursor(me, max(read_cursor(me), seq))

    print(f"sent → seq={seq}  to={recipient}  title={title!r}")


def cmd_watch(args):
    """Block until new records addressed to me arrive; print ALL of them.

    Exit codes:
        0 = new records printed.
        2 = cap elapsed with no new records.
    """
    ensure_dirs()
    interval = float(args[0]) if len(args) >= 1 else 5.0
    cap = float(args[1]) if len(args) >= 2 else 1200.0

    me = identity()
    start = time.time()
    start_cursor = read_cursor(me)

    print(f"[watch] identity={me} interval={interval}s cap={cap}s "
          f"log={LOG_PATH} cursor={start_cursor}")

    while True:
        elapsed = time.time() - start
        if elapsed >= cap:
            print(f"[watch] timeout after {cap:.0f}s with no new messages")
            sys.exit(2)

        new = [
            (seq, rec)
            for seq, rec in iter_records()
            if seq > start_cursor and addressed_to_me(rec, me)
        ]
        if new:
            print(f"[watch] {len(new)} new message(s) for {me}:")
            for seq, rec in new:
                print(f"  [{seq}] {rec.get('from', '?')} → {rec.get('to', '?')}"
                      f"  | {rec.get('title', '')}  ({rec.get('ts', '?')})")
                if rec.get("in_reply_to"):
                    print(f"        in_reply_to: {rec['in_reply_to']}")
            print(f"[watch] run: msg.sh read <seq>  to read any of the above")
            sys.exit(0)
        time.sleep(interval)


def cmd_whois(args):
    ensure_dirs()
    seen = set()
    for _, rec in iter_records():
        f = rec.get("from")
        if f:
            seen.add(f)
    if not seen:
        print("(no records yet)")
        return
    for f in sorted(seen):
        print(f)


def cmd_clean(args):
    """Rotate the log if it is older than 7 days. Keeps all history, just archives."""
    ensure_dirs()
    if not LOG_PATH.exists():
        print("no log to clean")
        return
    age_days = (time.time() - LOG_PATH.stat().st_mtime) / 86400.0
    if age_days <= 7:
        print(f"log is {age_days:.1f} days old — not rotating (threshold: 7d)")
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = MSG_DIR / f"events.{ts}.log"
    LOG_PATH.rename(archive)
    LOG_PATH.touch()
    # Clear cursors so agents start fresh against the new empty log.
    for cp in CURSOR_DIR.glob("*.txt"):
        cp.write_text("0")
    print(f"rotated → {archive}")


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    sub, *rest = argv
    handlers = {
        "whoami": cmd_whoami,
        "inbox": cmd_inbox,
        "ls": cmd_inbox,
        "read": cmd_read,
        "peek": cmd_peek,
        "mark-seen": cmd_mark_seen,
        "send": cmd_send,
        "watch": cmd_watch,
        "whois": cmd_whois,
        "clean": cmd_clean,
    }
    handler = handlers.get(sub)
    if handler is None:
        print(f"unknown subcommand: {sub}", file=sys.stderr)
        print(f"try: {' | '.join(handlers)}", file=sys.stderr)
        sys.exit(1)
    handler(rest)


if __name__ == "__main__":
    main()

---
name: agent-messages
description: Append-only log-based message bus between Cursor agents working on different projects on the same machine. Single shared log at /tmp/agent-messages/events.log with JSONL records; every message has a strictly monotonic sequence number and a causal `in_reply_to` field. Use when the user says "check your messages", "check messages", "check inbox", "go have a meeting", "meet with the other agents", "send a message to <project>", "reply to <project>", or any phrasing about async inter-agent communication. Agnostic — works unchanged in any project; the agent's identity is the basename of its workspace folder.
---

# Agent Messages

An append-only log for async communication between Cursor agents running on the same Mac (different projects, different sessions). One shared log file with JSONL records; each agent tracks its own read position as a single integer.

## The data model (one file, one cursor)

```
/tmp/agent-messages/
├── events.log                 ← the conversation, JSONL, append-only
└── .cursor/
    ├── <agent-a>.txt          ← integer: last seq this agent has read
    └── <agent-b>.txt
```

Each line in `events.log` is one message:

```json
{"seq": 42, "ts": "2026-04-17T18:00:00Z", "from": "agent-a", "to": "all",
 "title": "short snippet", "in_reply_to": 40, "body": "markdown body..."}
```

- **`seq`** is the 1-indexed line number. It is the message's identity and its position in causal order. Strictly monotonic.
- **`to`** is either a specific agent identity (DM) or the literal string `"all"` (broadcast). Agents filter on read.
- **`in_reply_to`** is the seq of the message this one responds to, or `null` for unprompted sends.
- **`body`** is markdown.

Appends are serialized by `fcntl.LOCK_EX` on `events.log`, so two senders sending at the same instant get distinct, ordered `seq`s. No collision. No lost record.

Each agent's cursor is a single integer in `.cursor/<id>.txt` — the highest seq they have read. To catch up, they read every record with `seq > cursor`.

## Why this shape (why we left the folder-of-files model)

The older model used one file per message in a shared folder. That has no causal ordering: mtimes are wall-clock, filenames are unordered, and per-agent `.seen` sets give you "what I have looked at" but not "what the conversation is at this point." Concurrent senders produced replies against stale state because there was no way to express "the conversation is through seq N, reply to that."

The log fixes it structurally. Collisions become impossible because every append lands at a strictly greater seq than every prior record. Stale-state replies become detectable because `in_reply_to: N` asserts the sender has read through at least seq `N`; if their cursor is behind, the send is rejected at source.

## The helper

One script: `scripts/msg.sh` (actually a Python 3 executable; name kept for back-compat). Always invoke with an explicit path from the repo root:

```bash
.cursor/skills/agent-messages/scripts/msg.sh <subcommand> [args...]
```

| Subcommand | Purpose |
|---|---|
| `whoami` | Print this agent's identity (workspace basename). |
| `inbox` | List unread + read records addressed to me or `all`, with seq numbers. |
| `inbox --all` | List every record (debugging). |
| `read <seq>` | Print one record and advance cursor to that seq. |
| `peek <seq>` | Print without advancing cursor. |
| `mark-seen <seq>` | Advance cursor to seq without printing. |
| `send <recipient> <title>` | Read body from stdin, append record. Optional `--in-reply-to <seq>` asserts causal link. |
| `watch [interval] [cap]` | Block until new records arrive for me; print ALL of them (not just first). Defaults: interval=5s, cap=1200s. |
| `whois` | Unique senders in the log. |
| `clean` | Rotate the log if older than 7 days (archive, keep history). |

## When to use

- **"check messages"** / **"check inbox"** / **"any messages?"** → single-shot: `inbox`, then `read` each unread by seq, summarize.
- **"go have a meeting"** / **"start a standup"** → interactive: check inbox, send an opening if asked, then poll with `watch` for replies.
- **"send a message to <project>"** / **"reply to <project>"** → send-only: write the message, done.

## How to use (agent protocol)

### Identify yourself first

Run `msg.sh whoami` at the start of any operation. Include your identity in summaries so the user can disambiguate multi-agent activity.

### Single-shot inbox check

1. `msg.sh inbox` — scan for unread records addressed to you or `all`.
2. For each unread, `msg.sh read <seq>` — prints and advances cursor.
3. Summarize: who sent what, ask what needs action. Do not auto-reply.

### Interactive meeting

1. `msg.sh whoami` and `msg.sh inbox` in the same batch. Read any unread with their seqs.
2. Send the opening: `echo "<body>" | msg.sh send <recipient> "<title>"`. Use `--in-reply-to <seq>` when responding to a specific prior message.
3. `msg.sh watch` — blocks until new records arrive. Returns ALL new records in that window (no more missed bursts). Read each with `msg.sh read <seq>`.
4. Draft a reply from context. Send. One-line "sent → seq=N" confirmation is the norm; do not narrate.
5. Loop 3–4 until the user says stop or `watch` hits its cap with no activity.
6. On end, give a short summary of exchanges + follow-ups owed.

### Always cite seq when replying

When your reply addresses a specific prior message, pass `--in-reply-to <seq>`. This is enforced: if your cursor is behind the referenced seq, `send` rejects with an error. You must read the referenced record first. That guarantee keeps the causal structure honest — no reply ever claims to respond to a message the sender hasn't actually seen.

### Watch yields batches, not single records

Unlike the old bus, `watch` does NOT exit on the first new message. It returns every new record for you since your cursor, so concurrent sends from multiple peers are all surfaced in one shot. Read them in seq order.

## Sending

- **`<recipient>`**: another agent's identity (workspace basename) or `"all"` for broadcast.
- **`<title>`**: 3–8-word summary. Goes into the record's `title` field. Surfaced in inbox listings.
- **Body**: markdown on stdin. Be explicit about what you want from the recipient (ack, decision, input, FYI).
- **`--in-reply-to <seq>`**: always use when responding to a specific message. Makes the thread explicit.
- **Just send.** Once the user has authorized a meeting, every send inside the loop is in-bounds. One-line `sent → seq=N` ack and move on.
- **Pause only for irreversible commitments.** Scheduling real meetings, promising dates, making architectural decisions the user hasn't made — those get confirmed. Everything else ships from context.

## Edge cases

- **Empty log**: first call creates `events.log` as empty and `inbox` reports zero records. No setup.
- **Malformed JSON line in log**: the reader prints a warning to stderr and skips that line. The seq numbering continues from line-count; you do not get a hole.
- **Self-broadcasts**: when you send to `all`, the cursor auto-advances past your own seq, so your own broadcast does not appear in your next `inbox` as unread.
- **Concurrent senders**: handled structurally by `fcntl.LOCK_EX` on the log. Both records get distinct, ordered seqs.
- **Cursor ahead of log end** (e.g., log was rotated): treated as "up to date"; no records will be yielded until new lines are appended.
- **Legacy `.md` files in `/tmp/agent-messages/` from the prior bus**: ignored. They stay on disk for reference but are not part of the log.

## Debugging

- `msg.sh inbox --all` — every record in the log regardless of recipient.
- `msg.sh whois` — which agents have posted.
- `cat /tmp/agent-messages/events.log | wc -l` — current seq count.
- `cat /tmp/agent-messages/.cursor/<my-id>.txt` — my current cursor.
- `head -n 20 /tmp/agent-messages/events.log | jq -r '.seq, .from, .to, .title'` — inspect raw records.

Never delete records from the log manually. `clean` rotates (moves to archive); it does not lose history.

## Portability

This skill is **agnostic**. Copy the entire `.cursor/skills/agent-messages/` folder as-is to any other Cursor project and it works immediately: the agent's identity is inferred from the workspace folder name, and `/tmp/agent-messages/events.log` is shared across all projects on the machine.

No project-specific configuration. No hardcoded project names.

## Example: a meeting

**User in api-apifunnel-ai**: "go have a meeting with the mcp team. tell them the auth plan is ready."

**Agent**:

```
$ msg.sh whoami
api-apifunnel-ai

$ msg.sh inbox
# Inbox for: api-apifunnel-ai
# Log: /tmp/agent-messages/events.log  (cursor = 0)

UNREAD:
  (none)
READ:
  (none)
Summary: 0 unread, 0 read.

$ echo "Auth unification plan landed at plans/rename-and-auth-unification/. When can you flip the bridge outbound shape?" | msg.sh send mcp-code-execution "auth plan ready - bridge flip timing"
sent → seq=1  to=mcp-code-execution  title='auth plan ready - bridge flip timing'

$ msg.sh watch 5 1200
[watch] identity=api-apifunnel-ai interval=5s cap=1200s log=/tmp/agent-messages/events.log cursor=1
...
[watch] 1 new message(s) for api-apifunnel-ai:
  [2] mcp-code-execution → api-apifunnel-ai  | bridge flip ack  (2026-04-17T11:31:04Z)
        in_reply_to: 1
[watch] run: msg.sh read <seq>  to read any of the above

$ msg.sh read 2
seq:          2
ts:           2026-04-17T11:31:04Z
from:         mcp-code-execution
to:           api-apifunnel-ai
title:        bridge flip ack
in_reply_to:  1
---
Plan read. Bridge flip ready next week after current sprint.
```

Agent to user: "Bridge team acknowledged (seq 2, replying to our seq 1). They'll flip next week. Want to reply 'thanks, standing by'?"

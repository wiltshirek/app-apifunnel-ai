---
name: agent-messages
description: File-based message bus between Cursor agents working on different projects on the same machine. Messages are plain markdown files in /tmp/agent-messages/ with naming convention <recipient>:<snippet>.md. Use when the user says "check your messages", "check messages", "check inbox", "go have a meeting", "meet with the other agents", "send a message to <project>", "reply to <project>", or any phrasing about async inter-agent communication. Agnostic — works unchanged in any project; the agent's identity is the basename of its workspace folder.
---

# Agent Messages

A minimal file-based inbox/outbox for async communication between Cursor agents running on the same Mac (different projects, different sessions). Messages are markdown files in a single shared tmp directory.

## The protocol

```
/tmp/agent-messages/
├── <recipient>:<snippet>.md             ← one message, body is free-form markdown
├── <recipient>:<snippet>.md
├── all:<snippet>.md                     ← broadcast to every agent
└── .seen/
    ├── <recipient-a>.txt                ← list of filenames this agent has processed
    └── <recipient-b>.txt
```

- **Identity**: each agent's identity is the `basename` of its workspace root (e.g. `api-apifunnel-ai`, `mcp-code-execution`, `one-mcp`). No config needed.
- **Filename**: `<recipient>:<snippet>.md`. Snippet is a short human-readable description of the message (whitespace + alphanumerics + basic punctuation; auto-sanitized by the helper).
- **Broadcast**: recipient `all` is picked up by every agent's inbox.
- **Body**: plain markdown, with a small YAML frontmatter block auto-added by the sender (`from`, `to`, `ts`).
- **Read state**: per-recipient `.seen/<recipient>.txt` line-per-filename. Marking a message "seen" is local to the reading agent; the file itself stays on disk so other recipients (for broadcasts) or later re-reads still work.
- **Persistence**: `/tmp/` survives between agent sessions but is cleared on reboot. Old messages (>7 days) should be purged with the `clean` subcommand occasionally.

## The helper

A single script `scripts/msg.sh` wraps every operation. Always invoke with an explicit path from the repo root so relative paths resolve:

```bash
.cursor/skills/agent-messages/scripts/msg.sh <subcommand> [args...]
```

Subcommands:

| Subcommand | Purpose |
|---|---|
| `whoami` | Print this agent's identity (workspace basename). |
| `inbox` | List unread + read messages addressed to me or `all`. Default. |
| `inbox --all` | List every message in the directory (debugging). |
| `read <file>` | Cat a message and mark it seen. |
| `peek <file>` | Cat a message without marking seen. |
| `send <recipient> <snippet>` | Read body from stdin, write a message. |
| `mark-seen <file>` | Mark a file seen without reading (for noisy broadcasts). |
| `watch [interval] [cap]` | Poll inbox, exit on first new unread message. Defaults: 15s interval, 1200s cap. |
| `whois` | List recipient prefixes currently present in the directory. |
| `clean` | Delete messages older than 7 days. |

Files addressed to the sender's own identity are auto-marked seen on send, so agents don't notify themselves via broadcasts.

## When to Use

Trigger this skill on any of:

- **"check messages"** / **"check your messages"** / **"check inbox"** / **"any messages?"** → single-shot: run `inbox`, `read` each unread, summarize. No polling.
- **"go have a meeting"** / **"meet with the other agents"** / **"start a meeting"** / **"open the channel"** → interactive: check inbox, possibly send an opening message if the user asked for one, then **poll** for replies (see below). Keep the meeting alive until the user says stop or no messages arrive for the watcher's cap.
- **"send a message to <project>"** / **"tell <project> X"** / **"reply to <project>"** / **"ack that"** → send-only: write a message, report the filename, done.
- **"share plans"** is a *different* skill (`share-plans`); don't conflate.

## Instructions

### Identify yourself first

Run `msg.sh whoami` at the start of any message operation. Reference this identity in responses so the user can disambiguate when multiple agents are active.

### For "check messages" (single-shot)

1. Run `msg.sh inbox`.
2. For each unread file, run `msg.sh read "<filename>"`.
3. Summarize: who sent what, action needed (if any). If a message requests a reply, ask the user whether to reply and with what content; do not auto-reply.
4. Done. No looping.

### For "go have a meeting" (interactive) — act, don't stall

Meetings are action mode. The user has already authorized participation. Stop asking "should I send?" or "what should I reply?" — draft and send from context. The user interrupts if they disagree.

1. Run `msg.sh whoami` and `msg.sh inbox`. Read any unread messages in the same tool call batch as the send (no round-trip back to the user just to report the inbox).
2. **Send immediately.** If the user provided an opening line, use it. Otherwise draft one from the current conversation context and send: `echo "<body>" | msg.sh send <recipient> "<snippet>"`. Announce what was sent in one line; do not ask first.
3. Enter a polling loop. Use `msg.sh watch <interval> <cap>` — defaults 15s / 1200s (20 minutes). When it prints `NEW MESSAGE: <filename>`, it exits; read the file with `msg.sh read "<filename>"`.
4. **Draft and send a reply from context.** Read the incoming message, compose a reply that addresses every ask directly, send it. Report what was sent in one line. Only pause for the user when the message asks for a decision the user hasn't made yet (dates, priorities, team commitments, anything requiring human judgment).
5. Loop 3–4 until either:
   - The user says "meeting over" / "stop" / "done" / similar.
   - `watch` hits its cap with no new messages → send one more "still here, anything to add?" or exit the loop with a summary (your call based on the meeting's pace).
6. On end, give a short summary of what was exchanged and any follow-up actions owed.

### Polling is required during a meeting

Do **not** just run `inbox` once and stop. A meeting is a back-and-forth; messages arrive at unpredictable intervals. Use `msg.sh watch` in a tight loop. If the Cursor runtime drops you out of a long-running `watch`, restart it immediately and report the brief gap to the user.

### Sending

- **Snippet**: 3–8 words that summarize the message. Examples: `"auth plan ack"`, `"q on rename timing"`, `"ready to ship bridge pr"`.
- **Body**: markdown prose. Be explicit about what you want from the recipient (ack, decision, input, FYI).
- **Threading**: when replying, include a `> On <timestamp>, <from> wrote:` quote or a `reply-to: <filename>` line in your body so the recipient has context.
- **Just send.** The default is to send. Once the user has kicked off the meeting, every send inside the loop is in-bounds. Do NOT quote-then-confirm-then-send — that pattern burns the user's attention. Send, then report `sent → <filename>` in one line.
- **Pause only for irreversible commitments.** Scheduling real meetings, promising dates, making architectural decisions the user hasn't made — those get confirmed. Everything else (acks, clarifications, status updates, technical answers derived from context) just ships.

### Edge cases

- **Empty inbox**: say so plainly. Don't invent activity.
- **Filename with spaces**: the helper handles colons and spaces already; always quote the filename when passing to `read` / `peek`: `msg.sh read "api-apifunnel-ai:my message.md"`.
- **Message addressed to `all` that I've seen**: `read` against it twice just re-cats; seen-state is already recorded.
- **Self-broadcasts**: messages you send to `all` don't show up in your own unread (you're auto-marked seen). This is intentional.
- **Directory doesn't exist yet**: `msg.sh` creates it on first use. No setup required.

### Debugging

- `msg.sh whois` — shows which recipients have messages in the dir.
- `msg.sh inbox --all` — shows every file regardless of recipient.
- `ls -la /tmp/agent-messages/` — raw directory listing.
- `cat /tmp/agent-messages/.seen/<my-id>.txt` — what I've marked as read.

Never delete files from `/tmp/agent-messages/` manually; use `msg.sh clean` instead, which only removes >7-day-old files.

## Portability

This skill is **agnostic**. Copy the entire `.cursor/skills/agent-messages/` folder as-is to any other Cursor project and it works immediately: the agent's identity is inferred from the workspace folder name, and `/tmp/agent-messages/` is shared across all projects on the machine.

No project-specific configuration. No hardcoded project names.

## Example: a meeting

**User in api-apifunnel-ai**: "go have a meeting with the mcp team. tell them the auth plan is ready and ask when they can flip the bridge."

**Agent**:
```
$ msg.sh whoami
api-apifunnel-ai

$ echo "The auth unification plan is ready at plans/rename-and-auth-unification/. When can you flip the bridge outbound shape?" | msg.sh send mcp-code-execution "auth plan ready — bridge flip timing?"
sent → mcp-code-execution:auth plan ready — bridge flip timing.md

$ msg.sh watch 15 1200
[watch] identity=api-apifunnel-ai interval=15s cap=1200s dir=/tmp/agent-messages
[watch] 07:30:15 no new messages (elapsed 0s)
...
[watch] NEW MESSAGE: api-apifunnel-ai:bridge flip ack.md

$ msg.sh read "api-apifunnel-ai:bridge flip ack.md"
=== api-apifunnel-ai:bridge flip ack.md ===
mtime: 2026-04-17 07:31:04 EDT
---
from: mcp-code-execution
to: api-apifunnel-ai
ts: 2026-04-17T11:31:04Z
---

Plan read. Bridge flip ready next week after current sprint. Will open a PR and link it here.
```

Agent to user: "Bridge team acknowledges. They'll flip next week after the current sprint, with a PR linked back. Want to reply 'thanks, standing by' or end the meeting?"

User: "end it".

Agent: meeting closed.

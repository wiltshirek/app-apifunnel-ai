#!/bin/bash
# Agent message bus — file-based inter-project communication.
#
# Shared inbox lives at /tmp/agent-messages/. Any agent on this machine with
# this skill installed can read & write here.
#
# Filename convention:      <recipient>:<snippet>.md
# Broadcast recipient:      all
# Per-recipient seen state: /tmp/agent-messages/.seen/<recipient>.txt
#
# Subcommands:
#   whoami                         → prints this agent's identity (workspace dir basename)
#   inbox [--all]                  → lists unread messages for me (or all files with --all)
#   read <file>                    → cat a message and mark it seen
#   peek <file>                    → cat a message without marking seen
#   send <recipient> <snippet>     → reads body from stdin, writes a message
#   mark-seen <file>               → mark a file as seen without reading
#   watch [interval] [cap]         → poll inbox every <interval>s until new msg arrives
#                                      or <cap>s elapses. interval=15, cap=1200 by default.
#   whois                          → lists recent senders (unique recipients that have appeared)
#   clean                          → deletes messages older than 7 days
#
# All identities are derived from the basename of the Cursor workspace root (i.e.
# $PWD when this script is invoked by the agent). No per-project config needed.

set -euo pipefail

MSG_DIR="/tmp/agent-messages"
SEEN_DIR="$MSG_DIR/.seen"

_ensure_dirs() {
    mkdir -p "$MSG_DIR" "$SEEN_DIR"
}

_identity() {
    # The agent's identity is the basename of its current workspace root.
    basename "$PWD"
}

_seen_file() {
    echo "$SEEN_DIR/$(_identity).txt"
}

_is_seen() {
    local fname="$1"
    local seen_file
    seen_file=$(_seen_file)
    [ -f "$seen_file" ] && grep -Fxq "$fname" "$seen_file"
}

_mark_seen() {
    local fname="$1"
    local seen_file
    seen_file=$(_seen_file)
    touch "$seen_file"
    if ! grep -Fxq "$fname" "$seen_file" 2>/dev/null; then
        echo "$fname" >> "$seen_file"
    fi
}

_list_for_me() {
    # Emit filenames (basenames) addressed to me or to "all", sorted by mtime ascending
    # (oldest first, so iteration naturally surfaces historical order).
    local me
    me=$(_identity)
    ( cd "$MSG_DIR" 2>/dev/null && ls -1tr 2>/dev/null ) \
      | grep -E "^(${me}|all):" 2>/dev/null || true
}

cmd_whoami() {
    _ensure_dirs
    _identity
}

cmd_inbox() {
    _ensure_dirs
    local show_all=0
    if [ "${1:-}" = "--all" ]; then show_all=1; fi

    if [ $show_all -eq 1 ]; then
        cd "$MSG_DIR"
        echo "# All messages in $MSG_DIR"
        ls -lht 2>/dev/null | tail -n +2 | awk '{print $6, $7, $8, $9}' | grep -E "\.md$" || echo "(empty)"
        return 0
    fi

    local me
    me=$(_identity)
    echo "# Inbox for: $me"
    echo "# Dir: $MSG_DIR"
    echo ""

    local files
    files=$(_list_for_me)
    if [ -z "$files" ]; then
        echo "(no messages addressed to $me or all)"
        return 0
    fi

    local unread=0
    local read_count=0
    echo "UNREAD:"
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if _is_seen "$f"; then
            read_count=$((read_count + 1))
        else
            unread=$((unread + 1))
            # show mtime + filename
            local mt
            mt=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$MSG_DIR/$f" 2>/dev/null || echo "??")
            echo "  [$mt]  $f"
        fi
    done <<< "$files"

    [ $unread -eq 0 ] && echo "  (none)"

    echo ""
    echo "READ (still on disk, use 'read' to re-open):"
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if _is_seen "$f"; then
            local mt
            mt=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$MSG_DIR/$f" 2>/dev/null || echo "??")
            echo "  [$mt]  $f"
        fi
    done <<< "$files"
    [ $read_count -eq 0 ] && echo "  (none)"

    echo ""
    echo "Summary: $unread unread, $read_count read."
}

cmd_read() {
    _ensure_dirs
    local fname="${1:?usage: msg.sh read <filename>}"
    local path="$MSG_DIR/$fname"
    [ -f "$path" ] || { echo "no such message: $fname" >&2; exit 1; }
    echo "=== $fname ==="
    stat -f "mtime: %Sm" -t "%Y-%m-%d %H:%M:%S %Z" "$path"
    echo "---"
    cat "$path"
    echo ""
    _mark_seen "$fname"
    echo "(marked seen)"
}

cmd_peek() {
    _ensure_dirs
    local fname="${1:?usage: msg.sh peek <filename>}"
    local path="$MSG_DIR/$fname"
    [ -f "$path" ] || { echo "no such message: $fname" >&2; exit 1; }
    cat "$path"
}

cmd_send() {
    _ensure_dirs
    local recipient="${1:?usage: msg.sh send <recipient> <snippet>  (body via stdin)}"
    local snippet="${2:?usage: msg.sh send <recipient> <snippet>  (body via stdin)}"
    local from
    from=$(_identity)

    # Sanitize snippet: keep letters, numbers, dash, underscore, dot, space; trim.
    local safe_snippet
    safe_snippet=$(echo "$snippet" | tr -cs 'A-Za-z0-9._ -' '-' | sed 's/^-*//;s/-*$//' | cut -c 1-80)
    [ -z "$safe_snippet" ] && safe_snippet="message"

    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    local fname="${recipient}:${safe_snippet}.md"
    local path="$MSG_DIR/$fname"

    # If colliding, append a numeric suffix.
    local suffix=1
    while [ -e "$path" ]; do
        fname="${recipient}:${safe_snippet}-${suffix}.md"
        path="$MSG_DIR/$fname"
        suffix=$((suffix + 1))
    done

    {
        echo "---"
        echo "from: $from"
        echo "to: $recipient"
        echo "ts: $ts"
        echo "---"
        echo ""
        cat  # body from stdin
    } > "$path"

    # Also mark this file as seen by the sender so it doesn't appear in their own inbox
    # if they happen to also match the recipient (e.g. sent to 'all').
    _mark_seen "$fname"

    echo "sent → $fname"
}

cmd_mark_seen() {
    _ensure_dirs
    local fname="${1:?usage: msg.sh mark-seen <filename>}"
    _mark_seen "$fname"
    echo "marked seen: $fname"
}

cmd_watch() {
    _ensure_dirs
    local interval="${1:-15}"
    local cap="${2:-1200}"
    local me
    me=$(_identity)
    local start
    start=$(date +%s)

    echo "[watch] identity=$me interval=${interval}s cap=${cap}s dir=$MSG_DIR"

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$((now - start))
        if [ $elapsed -ge "$cap" ]; then
            echo "[watch] timeout after ${cap}s without new messages"
            exit 2
        fi

        # Look for any unread file for this recipient.
        local new_found=""
        local files
        files=$(_list_for_me)
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            if ! _is_seen "$f"; then
                new_found="$f"
                break
            fi
        done <<< "$files"

        if [ -n "$new_found" ]; then
            echo "[watch] NEW MESSAGE: $new_found"
            echo "[watch] run: msg.sh read \"$new_found\""
            exit 0
        fi

        printf "[watch] %s no new messages (elapsed %ss)\n" "$(date '+%H:%M:%S')" "$elapsed"
        sleep "$interval"
    done
}

cmd_whois() {
    _ensure_dirs
    cd "$MSG_DIR"
    # Collect recipients (prefix of every message filename).
    ls -1 2>/dev/null | grep -E ":.*\.md$" | cut -d: -f1 | sort -u \
      | grep -v '^$' || echo "(no messages yet)"
}

cmd_clean() {
    _ensure_dirs
    local removed
    removed=$(find "$MSG_DIR" -maxdepth 1 -name "*.md" -type f -mtime +7 -print -delete 2>/dev/null | wc -l | tr -d ' ')
    echo "cleaned $removed messages older than 7 days"
}

main() {
    local sub="${1:-inbox}"
    shift || true
    case "$sub" in
        whoami)     cmd_whoami "$@" ;;
        inbox|ls)   cmd_inbox "$@" ;;
        read)       cmd_read "$@" ;;
        peek)       cmd_peek "$@" ;;
        send)       cmd_send "$@" ;;
        mark-seen)  cmd_mark_seen "$@" ;;
        watch)      cmd_watch "$@" ;;
        whois)      cmd_whois "$@" ;;
        clean)      cmd_clean "$@" ;;
        -h|--help|help)
            # Print only the top-of-file comment block (everything up to the first blank line
            # after `set -euo pipefail` marker).
            awk '/^set -euo pipefail/{exit} {print}' "$0"
            ;;
        *)
            echo "unknown subcommand: $sub" >&2
            echo "try: whoami | inbox | read | peek | send | mark-seen | watch | whois | clean" >&2
            exit 1
            ;;
    esac
}

main "$@"

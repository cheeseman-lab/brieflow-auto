#!/bin/bash
# brieflow_notify.sh — Slack-only notifier with priority tiers.
#
# Usage:
#   bash brieflow_notify.sh <tag> <message> [priority]
#
# Args:
#   tag      Run tag (matches the runloop's --tag).
#   message  Body text. If prefixed with [info] / [warning] /
#            [action_needed], the prefix is stripped and used as priority.
#   priority info | progress | warning | action_needed   (default: info)
#
# Env vars (read once per invocation):
#   BRIEFLOW_SLACK_WEBHOOK=https://hooks...   # Slack incoming-webhook URL
#   BRIEFLOW_NOTIFY_THROTTLE_SECONDS=900      # warning throttle, default 15min
#   BRIEFLOW_NOTIFY_STATE_DIR=~/.cache/brieflow_notify
#
# Priority routing:
#   info          — log only (no push)
#   progress      — log + Slack immediately, no throttle, low urgency
#                   (milestone/heartbeat; a missing channel is not an error)
#   warning       — log + Slack, throttled per (tag, priority)
#   action_needed — log + Slack immediately, no throttle
#
# Channel: Slack if BRIEFLOW_SLACK_WEBHOOK is set, else log-only.
#
# State (audit log + throttle timestamps) lives under
# BRIEFLOW_NOTIFY_STATE_DIR (default ~/.cache/brieflow_notify/). Never
# writes to /tmp — shared-HPC policy.

set -u

# Explicit offline mode for tests and candidate-harness evaluation. Check before
# reading the user's environment file or writing into their notification state.
case "${BRIEFLOW_NOTIFY_MODE:-normal}" in
    disabled) exit 0 ;;
    normal) ;;
    *) echo "brieflow_notify: invalid BRIEFLOW_NOTIFY_MODE" >&2; exit 2 ;;
esac

TAG="${1:-unknown_tag}"
MESSAGE="${2:-Pipeline notification}"
PRIORITY="${3:-info}"

# Allow the caller (e.g. watchdog) to encode priority as a "[priority] "
# prefix on the message. Strip it and override the priority arg.
case "$MESSAGE" in
    "[info] "*)          PRIORITY=info;          MESSAGE="${MESSAGE#\[info\] }" ;;
    "[progress] "*)      PRIORITY=progress;      MESSAGE="${MESSAGE#\[progress\] }" ;;
    "[warning] "*)       PRIORITY=warning;       MESSAGE="${MESSAGE#\[warning\] }" ;;
    "[action_needed] "*) PRIORITY=action_needed; MESSAGE="${MESSAGE#\[action_needed\] }" ;;
esac

case "$PRIORITY" in
    info|progress|warning|action_needed) ;;
    *) PRIORITY=info ;;
esac

STATE_DIR="${BRIEFLOW_NOTIFY_STATE_DIR:-${HOME}/.cache/brieflow_notify}"
mkdir -p "$STATE_DIR"

HOST=$(hostname)
SCREEN="${BRIEFLOW_SCREEN:-}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
UPPER_PRIORITY=$(echo "$PRIORITY" | tr '[:lower:]' '[:upper:]' | sed 's/_/-/g')

# Read the webhook fresh each invocation. ~/.brieflow_env is sourced only by
# interactive shells (via ~/.bashrc), so a runloop/notifier launched
# non-interactively — or started before the webhook was added — won't inherit it.
# Sourcing here decouples delivery from launch-time shell inheritance and lets
# Slack be enabled/rotated without restarting a run in progress.
NOTIFY_ENV_FILE="${BRIEFLOW_NOTIFY_ENV_FILE-${HOME}/.brieflow_env}"
if [ -z "${BRIEFLOW_SLACK_WEBHOOK:-}" ] && [ -n "$NOTIFY_ENV_FILE" ] && [ -f "$NOTIFY_ENV_FILE" ]; then
    # shellcheck disable=SC1090,SC1091
    . "$NOTIFY_ENV_FILE" 2>/dev/null || true
fi

# Channel: Slack if BRIEFLOW_SLACK_WEBHOOK is set, else log-only.
if [ -n "${BRIEFLOW_SLACK_WEBHOOK:-}" ]; then
    BRIEFLOW_NOTIFY_CHANNELS="slack"
else
    BRIEFLOW_NOTIFY_CHANNELS="log"
fi

THROTTLE_SEC="${BRIEFLOW_NOTIFY_THROTTLE_SECONDS:-900}"

# Audit log — always written, regardless of priority or channels.
AUDIT_LOG="${STATE_DIR}/notify.log"
printf '[%s] [%s] screen=%s tag=%s host=%s msg=%s\n' \
    "$TIMESTAMP" "$UPPER_PRIORITY" "${SCREEN:-?}" "$TAG" "$HOST" "$MESSAGE" >> "$AUDIT_LOG"

# info: log-only, exit.
if [ "$PRIORITY" = "info" ]; then
    exit 0
fi

# progress is a best-effort heartbeat — a missing channel is NOT a contract
# violation (unlike a warning/action_needed page), so log it and exit cleanly.
if [ "$PRIORITY" = "progress" ] && [ "$BRIEFLOW_NOTIFY_CHANNELS" = "log" ]; then
    exit 0
fi

# A warning/action_needed page with no delivery channel must NOT look delivered.
# Previously this fell through to a silent log-only `exit 0`, so a dropped page was
# invisible to the operator whose entire contract is "page me on a real decision".
if [ "$BRIEFLOW_NOTIFY_CHANNELS" = "log" ]; then
    printf '[%s] UNDELIVERABLE tag=%s — %s page but no channel (BRIEFLOW_SLACK_WEBHOOK unset)\n' \
        "$TIMESTAMP" "$TAG" "$UPPER_PRIORITY" >> "$AUDIT_LOG"
    echo "brieflow_notify: $UPPER_PRIORITY page for tag=$TAG could NOT be delivered —" \
         "BRIEFLOW_SLACK_WEBHOOK is unset (no Slack channel). Set it in ~/.brieflow_env." >&2
    exit 3
fi

# Throttle warnings to 1 / THROTTLE_SEC per tag. action_needed bypasses.
if [ "$PRIORITY" = "warning" ]; then
    safe_tag=$(echo "$TAG" | sed 's/[^A-Za-z0-9_-]/_/g')
    throttle_file="${STATE_DIR}/throttle_${safe_tag}.ts"
    now=$(date +%s)
    if [ -f "$throttle_file" ]; then
        last=$(cat "$throttle_file" 2>/dev/null || echo 0)
        delta=$((now - last))
        if [ "$delta" -lt "$THROTTLE_SEC" ]; then
            printf '[%s] THROTTLED tag=%s delta=%ds<%ds\n' \
                "$TIMESTAMP" "$TAG" "$delta" "$THROTTLE_SEC" >> "$AUDIT_LOG"
            exit 0
        fi
    fi
fi

# Slack — primary push channel when BRIEFLOW_SLACK_WEBHOOK is set.
if printf '%s\n' "$BRIEFLOW_NOTIFY_CHANNELS" | grep -q 'slack'; then
    emoji="info"
    [ "$PRIORITY" = "progress" ] && emoji="progress"
    [ "$PRIORITY" = "warning" ] && emoji="warning"
    [ "$PRIORITY" = "action_needed" ] && emoji="ACTION NEEDED"
    if [ -n "$SCREEN" ]; then
        raw_msg=$(printf '*brieflow [%s] %s / %s* %s' "$emoji" "$SCREEN" "$TAG" "$MESSAGE")
    else
        raw_msg=$(printf '*brieflow [%s] %s* %s' "$emoji" "$TAG" "$MESSAGE")
    fi
    # JSON-escape: backslash → \\, double-quote → \", newline → \n
    json_text=$(printf '%s' "$raw_msg" \
        | sed 's/\\/\\\\/g; s/"/\\"/g' \
        | awk '{printf "%s\\n", $0}' \
        | sed 's/\\n$//')
    # Capture the HTTP status: `curl -s` (no -f) exits 0 even on 404/403, so a
    # dead webhook would otherwise log "sent slack" falsely. Verify a 200 so the
    # audit line is honest (the page-path self-test reads this verdict).
    http_code=$(curl --connect-timeout 5 --max-time 15 -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"${json_text}\"}" \
        "$BRIEFLOW_SLACK_WEBHOOK" 2>/dev/null)
    if [ "$http_code" = "200" ]; then
        printf '[%s] sent slack tag=%s\n' "$TIMESTAMP" "$TAG" >> "$AUDIT_LOG"
        # A failed delivery must not suppress the next warning attempt.
        if [ "$PRIORITY" = "warning" ]; then
            echo "$now" > "$throttle_file"
        fi
    else
        printf '[%s] SLACK FAILED tag=%s http=%s\n' "$TIMESTAMP" "$TAG" "${http_code:-000}" >> "$AUDIT_LOG"
        exit 1
    fi
fi

exit 0

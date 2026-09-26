#!/bin/sh
set -eu

heartbeat_path="${1:-/tmp/startunnel-maintenance-heartbeat}"
max_age_seconds="${2:-180}"
current_time="${3:-}"

case "$max_age_seconds" in
    ''|*[!0-9]*) exit 1 ;;
esac

if [ -z "$current_time" ]; then
    current_time="$(date +%s)"
else
    case "$current_time" in
        *[!0-9]*) exit 1 ;;
    esac
fi

[ -f "$heartbeat_path" ] || exit 1
if modified_time="$(stat -c %Y -- "$heartbeat_path" 2>/dev/null)"; then
    :
elif modified_time="$(stat -f %m "$heartbeat_path" 2>/dev/null)"; then
    :
else
    exit 1
fi
case "$modified_time" in
    ''|*[!0-9]*) exit 1 ;;
esac
age_seconds=$((current_time - modified_time))

[ "$age_seconds" -ge 0 ] && [ "$age_seconds" -le "$max_age_seconds" ]

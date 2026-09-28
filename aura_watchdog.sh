#!/bin/sh

DIR="/home/auracom/aura"
SERVICE="$DIR/aura_service.sh"
PIDFILE="$DIR/aura.pid"
LOGFILE="$DIR/aura_watchdog.log"
LOCKFILE="$DIR/aura_watchdog.lock"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOGFILE"
}

if [ -f "$LOCKFILE" ]; then
    exit 0
fi

touch "$LOCKFILE"

cleanup() {
    rm -f "$LOCKFILE"
}

trap cleanup EXIT INT TERM

# Check the PID recorded by the service.
if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE" 2>/dev/null)"

    case "$PID" in
        ''|*[!0-9]*)
            log "Invalid PID file content: $PID"
            rm -f "$PIDFILE"
            PID=""
            ;;
    esac
fi

# If PID exists, verify that this PID is actually Aura.
if [ -n "$PID" ] && [ -r "/proc/$PID/cmdline" ]; then
    CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null)"

    case "$CMDLINE" in
        *"$DIR/bot.py"*)
            exit 0
            ;;
    esac

    log "PID $PID is not Aura. Removing stale PID file."
    rm -f "$PIDFILE"
fi

# PID file is missing/stale. Before starting, check whether Aura
# already exists independently of the PID file.
AURA_PID="$(pgrep -f "$DIR/bot.py" | head -n 1)"

if [ -n "$AURA_PID" ]; then
    log "Aura already running with PID $AURA_PID; synchronizing PID file."
    echo "$AURA_PID" > "$PIDFILE"
    exit 0
fi

log "Aura is DOWN; attempting automatic restart."

"$SERVICE" start >> "$LOGFILE" 2>&1

sleep 3

if [ -f "$PIDFILE" ]; then
    NEW_PID="$(cat "$PIDFILE" 2>/dev/null)"

    if [ -n "$NEW_PID" ] && [ -r "/proc/$NEW_PID/cmdline" ]; then
        NEW_CMDLINE="$(tr '\0' ' ' < "/proc/$NEW_PID/cmdline" 2>/dev/null)"

        case "$NEW_CMDLINE" in
            *"$DIR/bot.py"*)
                log "Aura successfully restarted with PID $NEW_PID."
                exit 0
                ;;
        esac
    fi
fi

log "WARNING: Aura restart verification failed."

exit 0

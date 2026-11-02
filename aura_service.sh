#!/bin/sh

### BEGIN INIT INFO
# Provides:          aura
# Required-Start:    $remote_fs $network
# Required-Stop:     $remote_fs $network
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Aura Telegram Bot
### END INIT INFO

NAME="aura"

# Portable paths. Nothing is hard-coded to one host any more:
#   AURA_DIR     project directory that contains bot.py
#                (default: the directory holding this script)
#   AURA_PYTHON  interpreter to run (default: the project virtualenv, else python3)
#   AURA_PIDFILE / AURA_LOGFILE  optional overrides for runtime files
# Put runtime files outside the checkout with AURA_PIDFILE/AURA_LOGFILE; set
# these in the environment or in /etc/default/aura instead of editing this file.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
DIR="${AURA_DIR:-$SCRIPT_DIR}"

if [ -n "${AURA_PYTHON:-}" ]; then
    PYTHON="$AURA_PYTHON"
elif [ -x "$DIR/.venv/bin/python" ]; then
    PYTHON="$DIR/.venv/bin/python"
elif [ -x "$DIR/venv/bin/python" ]; then
    PYTHON="$DIR/venv/bin/python"
else
    PYTHON=$(command -v python3 || command -v python)
fi

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    echo "No usable Python interpreter found. Set AURA_PYTHON to one." >&2
    exit 1
fi

SCRIPT="$DIR/bot.py"
PIDFILE="${AURA_PIDFILE:-$DIR/aura.pid}"
LOGFILE="${AURA_LOGFILE:-$DIR/aura.log}"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Aura is already running."
        return 0
    fi

    if [ ! -f "$SCRIPT" ]; then
        echo "Cannot start Aura: $SCRIPT does not exist." >&2
        return 1
    fi

    cd "$DIR" || exit 1

    nohup "$PYTHON" "$SCRIPT" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"

    echo "Aura started."
}

stop() {
    if [ ! -f "$PIDFILE" ]; then
        echo "Aura is not running."
        return 0
    fi

    PID=$(cat "$PIDFILE")

    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Aura stopped."
    else
        echo "Aura process not found."
    fi

    rm -f "$PIDFILE"
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Aura is running."
        return 0
    fi

    echo "Aura is not running."
    return 3
}

case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 2
        start
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

# Preserve the action's own exit status: `status` must return 3 when Aura is
# stopped so callers such as monitoring or `set -e` scripts see the truth.
exit $?

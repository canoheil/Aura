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
DIR="/home/auracom/aura"
PYTHON="/home/auracom/virtualenv/aura/3.11/bin/python"
SCRIPT="$DIR/bot.py"
PIDFILE="$DIR/aura.pid"
LOGFILE="$DIR/aura.log"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Aura is already running."
        return 0
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

exit 0

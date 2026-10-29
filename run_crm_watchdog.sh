#!/bin/zsh
set -u

APP_DIR="/Volumes/Zhang/AAIE CodeX/CRM"
PYTHON="/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
LOG_FILE="$APP_DIR/logs/watchdog.launcher.log"

mkdir -p "$APP_DIR/logs"

log_line() {
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') | $1" >> "$LOG_FILE"
}

log_line "watchdog launcher starting"

if [[ ! -x "$PYTHON" ]]; then
  log_line "python not executable: $PYTHON"
  exit 78
fi

if [[ ! -f "$APP_DIR/watchdog.py" ]]; then
  log_line "watchdog.py missing: $APP_DIR/watchdog.py"
  exit 78
fi

cd "$APP_DIR" || {
  log_line "cannot cd to app dir: $APP_DIR"
  exit 78
}

log_line "start watchdog.py with $PYTHON"
"$PYTHON" "$APP_DIR/watchdog.py"
STATUS=$?
log_line "watchdog.py exited with status=$STATUS"
exit "$STATUS"

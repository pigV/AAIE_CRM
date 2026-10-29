#!/bin/zsh
set -u

APP_DIR="/Volumes/Zhang/AAIE CodeX/CRM"
SUPPORT_DIR="$HOME/Library/Application Support/AAIE CRM"
WATCHDOG_SCRIPT="$SUPPORT_DIR/watchdog.py"
WATCHDOG_PYTHON="/usr/bin/python3"
SERVICE_PYTHON="/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
LOG_DIR="$HOME/Library/Logs/AAIE CRM"
LOG_FILE="$LOG_DIR/watchdog.launcher.log"

mkdir -p "$LOG_DIR"

log_line() {
  /bin/date '+%Y-%m-%dT%H:%M:%S%z' | {
    read STAMP
    echo "$STAMP | $1" >> "$LOG_FILE"
  }
}

log_line "launchd wrapper starting"
log_line "app dir: $APP_DIR"

if [[ ! -d "$APP_DIR" ]]; then
  log_line "app dir is not accessible"
  /bin/ls -ld /Volumes /Volumes/Zhang "/Volumes/Zhang/AAIE CodeX" "$APP_DIR" >> "$LOG_FILE" 2>&1 || true
  exit 78
fi

if [[ ! -x "$WATCHDOG_PYTHON" ]]; then
  log_line "watchdog python is not executable: $WATCHDOG_PYTHON"
  /bin/ls -l "$WATCHDOG_PYTHON" >> "$LOG_FILE" 2>&1 || true
  exit 78
fi

if [[ ! -x "$SERVICE_PYTHON" ]]; then
  log_line "service python is not executable: $SERVICE_PYTHON"
  /bin/ls -l "$SERVICE_PYTHON" >> "$LOG_FILE" 2>&1 || true
  exit 78
fi

if [[ ! -f "$WATCHDOG_SCRIPT" ]]; then
  log_line "launchd watchdog copy missing: $WATCHDOG_SCRIPT"
  /bin/ls -l "$WATCHDOG_SCRIPT" >> "$LOG_FILE" 2>&1 || true
  exit 78
fi

cd "$APP_DIR" || {
  log_line "failed to cd to app dir"
  exit 78
}

export AAIE_CRM_PYTHON="$SERVICE_PYTHON"
export AAIE_CRM_APP_DIR="$APP_DIR"
export AAIE_CRM_WATCHDOG_STATE_DIR="$LOG_DIR"
export AAIE_CRM_SERVICE_LOG_DIR="$LOG_DIR"
export PYTHONDONTWRITEBYTECODE=1

log_line "starting watchdog.py with $WATCHDOG_PYTHON; service python $SERVICE_PYTHON"
"$WATCHDOG_PYTHON" "$WATCHDOG_SCRIPT"
STATUS=$?
log_line "watchdog.py exited with status=$STATUS"
exit "$STATUS"

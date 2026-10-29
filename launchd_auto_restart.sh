#!/bin/zsh
set -u

APP_DIR="/Volumes/Zhang/AAIE CodeX/CRM"
PYTHON="/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
REQUEST_FILE="$APP_DIR/logs/restart.request"
HANDLED_FILE="$APP_DIR/logs/restart.handled"

cd "$APP_DIR"
mkdir -p logs

if [[ ! -s "$REQUEST_FILE" ]]; then
  exit 0
fi

if [[ -f "$HANDLED_FILE" && ! "$REQUEST_FILE" -nt "$HANDLED_FILE" ]]; then
  exit 0
fi

echo "$(date '+%Y-%m-%dT%H:%M:%S%z') | launchd auto restart triggered" >> logs/launchd_auto_restart.out.log
"$PYTHON" "$APP_DIR/auto_restart_crm.py" --timeout 75 >> logs/launchd_auto_restart.out.log 2>> logs/launchd_auto_restart.err.log
STATUS=$?
/usr/bin/touch -r "$REQUEST_FILE" "$HANDLED_FILE" 2>/dev/null || /usr/bin/touch "$HANDLED_FILE"
exit "$STATUS"

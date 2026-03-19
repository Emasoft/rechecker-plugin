#!/usr/bin/env bash
# log-stop-failure.sh - StopFailure hook handler
# Logs transient API errors (rate limits, server errors) for awareness.
# StopFailure is notification-only: exit codes and output are ignored.
set -eu
set -o pipefail 2>/dev/null || true

INPUT=$(cat)

ERROR=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','unknown'))" 2>/dev/null || echo "unknown")
ERROR_DETAILS=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error_details',''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

# Log to a file in the project's reports_dev/ directory
LOG_DIR="${CWD}/reports_dev"
if [ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR" 2>/dev/null; then
    {
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] StopFailure: error=${ERROR} details=${ERROR_DETAILS} session=${SESSION_ID}"
    } >> "${LOG_DIR}/rechecker_api_errors.log"
fi

exit 0

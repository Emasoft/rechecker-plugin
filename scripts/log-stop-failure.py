#!/usr/bin/env python3
"""StopFailure hook handler - logs API errors.

Logs transient API errors (rate limits, server errors) for awareness.
StopFailure is notification-only: exit codes and output are ignored.
"""
import json
import sys
from datetime import datetime
from pathlib import Path


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = {}

    error = data.get("error", "unknown")
    error_details = data.get("error_details", "")
    session_id = data.get("session_id", "")
    cwd = data.get("cwd", "")

    if not cwd:
        sys.exit(0)

    log_dir = Path(cwd) / "reports_dev"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "rechecker_api_errors.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a") as f:
            f.write(f"[{ts}] StopFailure: error={error} details={error_details} session={session_id}\n")
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()

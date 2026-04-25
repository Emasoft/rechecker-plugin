#!/usr/bin/env python3
"""StopFailure hook handler - logs API errors.

Logs transient API errors (rate limits, server errors) for awareness.
StopFailure is notification-only: exit codes and output are ignored.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _resolve_main_root(cwd: str) -> Path:
    """Main-repo root (same whether we run in the main checkout or a worktree)."""
    try:
        out = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True, text=True, check=True, timeout=10, cwd=cwd,
        ).stdout
        return Path(out.splitlines()[0].split()[0])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError, OSError):
        return Path(cwd)


def main() -> None:
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

    # All rechecker-plugin output is rooted under reports/rechecker/ so the
    # whole plugin's footprint is one path the user can find, back up, or
    # clean — even when the hook fires inside a linked worktree (always
    # the main-repo root, never the worktree's own ./reports/).
    main_root = _resolve_main_root(cwd)
    log_dir = main_root / "reports" / "rechecker" / "stop-failure"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S%z")
        log_file = log_dir / f"{ts}-api-errors.log"
        with open(log_file, "a") as f:
            f.write(f"[{ts}] StopFailure: error={error} details={error_details} session={session_id}\n")
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()

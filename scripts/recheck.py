#!/usr/bin/env python3
"""recheck.py - On-demand review loop trigger.

Same logic as rechecker.py but without the PostToolUse JSON parsing.
Called directly: python3 recheck.py [commit_sha]
"""

import atexit
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    commit_sha = sys.argv[1] if len(sys.argv) > 1 else ""
    project_dir = os.getcwd()

    # Resolve commit
    if not commit_sha:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        commit_sha = r.stdout.strip() if r.returncode == 0 else ""

    if not commit_sha:
        print("ERROR: No commit found. Are you in a git repository?", file=sys.stderr)
        sys.exit(1)

    r = subprocess.run(["git", "cat-file", "-t", commit_sha], capture_output=True)
    if r.returncode != 0:
        print(f"ERROR: Commit not found: {commit_sha}", file=sys.stderr)
        sys.exit(1)

    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    )
    current_branch = r.stdout.strip() if r.returncode == 0 else "main"

    reports_dir = Path(project_dir) / "reports_dev"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plugin_root = os.environ.get(
        "CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent)
    )

    # Acquire lock
    lock_dir = Path(project_dir) / ".rechecker"
    lock_file = lock_dir / "rechecker.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            lock_pid = int(lock_file.read_text().strip())
            os.kill(lock_pid, 0)
            print(
                f"Another review cycle is already in progress (PID: {lock_pid}). Skipping."
            )
            sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            lock_file.unlink(missing_ok=True)

    lock_file.write_text(str(os.getpid()))

    def cleanup(*_args):
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    signal.signal(signal.SIGINT, lambda *a: (cleanup(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *a: (cleanup(), sys.exit(143)))
    atexit.register(cleanup)

    # Run the review loop
    reports_dir.mkdir(parents=True, exist_ok=True)
    review_loop_script = Path(plugin_root) / "scripts" / "review-loop.py"

    try:
        r = subprocess.run(
            [
                sys.executable,
                str(review_loop_script),
                project_dir,
                commit_sha,
                current_branch,
                str(reports_dir),
                timestamp,
                plugin_root,
            ],
            capture_output=True,
            text=True,
        )
        result = (
            r.stdout.strip() if r.stdout.strip() else "Review loop failed or timed out."
        )
    except Exception:
        result = "Review loop failed or timed out."

    print(result)


if __name__ == "__main__":
    main()

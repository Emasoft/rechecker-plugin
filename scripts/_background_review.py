#!/usr/bin/env python3
"""Background review runner. Launched by rechecker.py to avoid blocking the hook.

Takes git_root and plugin_root as args, acquires lock, runs full two-phase review.
Runs detached from the parent process (start_new_session=True).
"""

import subprocess
import sys
from pathlib import Path

from _shared import check_and_acquire_lock, run_two_phase_review, setup_lock_cleanup


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: _background_review.py <git_root> <plugin_root>", file=sys.stderr)
        sys.exit(1)

    git_root = sys.argv[1]
    plugin_root = sys.argv[2]

    # Acquire lock (exit silently if another review is running)
    lock_file, acquired = check_and_acquire_lock(git_root)
    if not acquired:
        sys.exit(0)
    setup_lock_cleanup(lock_file)

    # Get commit info
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=git_root)
    commit_sha = head.stdout.strip() if head.returncode == 0 else ""
    if not commit_sha:
        sys.exit(0)

    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=git_root)
    current_branch = branch.stdout.strip() if branch.returncode == 0 else "main"
    reports_dir = str(Path(git_root) / "reports_dev")

    # Run the full two-phase review
    run_two_phase_review(git_root, commit_sha, current_branch, reports_dir, plugin_root)


if __name__ == "__main__":
    main()

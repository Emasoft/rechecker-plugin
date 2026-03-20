#!/usr/bin/env python3
"""recheck.py - On-demand review loop trigger.

Same two-phase pipeline as rechecker.py but without the PostToolUse JSON parsing.
Called directly: python3 recheck.py [commit_sha]
"""

import os
import subprocess
import sys
from pathlib import Path

from _shared import check_and_acquire_lock, is_claude_available, run_two_phase_review, setup_lock_cleanup


def main() -> None:
    commit_sha = sys.argv[1] if len(sys.argv) > 1 else ""
    project_dir = os.getcwd()

    # Resolve commit
    if not commit_sha:
        head_result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        commit_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""

    if not commit_sha:
        print("ERROR: No commit found. Are you in a git repository?", file=sys.stderr)
        sys.exit(1)

    cat_result = subprocess.run(["git", "cat-file", "-t", commit_sha], capture_output=True)
    if cat_result.returncode != 0:
        print(f"ERROR: Commit not found: {commit_sha}", file=sys.stderr)
        sys.exit(1)

    # Verify claude CLI is available (shutil.which handles Windows .exe/.cmd)
    if not is_claude_available():
        print("ERROR: 'claude' CLI not found on PATH. Cannot run automated review.", file=sys.stderr)
        sys.exit(1)

    branch_result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    reports_dir = str(Path(project_dir) / "reports_dev")
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    # Acquire lock
    lock_file, acquired = check_and_acquire_lock(project_dir)
    if not acquired:
        print("Another review cycle is already in progress. Skipping.")
        sys.exit(0)
    setup_lock_cleanup(lock_file)

    # Run two-phase review pipeline
    phase1_result, phase2_result = run_two_phase_review(
        project_dir, commit_sha, current_branch, reports_dir, plugin_root
    )

    if phase2_result:
        print(f"[Phase 1 - Code Review] {phase1_result}")
        print(f"[Phase 2 - Functionality Review] {phase2_result}")
    else:
        print(phase1_result)


if __name__ == "__main__":
    main()

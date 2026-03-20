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

    branch_result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    reports_dir = Path(project_dir) / "reports_dev"
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    # Acquire lock
    lock_dir = Path(project_dir) / ".rechecker"
    lock_file = lock_dir / "rechecker.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            lock_pid = int(lock_file.read_text().strip())
            os.kill(lock_pid, 0)
            print(f"Another review cycle is already in progress (PID: {lock_pid}). Skipping.")
            sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            lock_file.unlink(missing_ok=True)

    lock_file.write_text(str(os.getpid()))

    def cleanup() -> None:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _handle_int(_s: int, _f: object) -> None:
        cleanup()
        sys.exit(130)

    def _handle_term(_s: int, _f: object) -> None:
        cleanup()
        sys.exit(143)

    signal.signal(signal.SIGINT, _handle_int)
    signal.signal(signal.SIGTERM, _handle_term)
    atexit.register(cleanup)

    # Run the review loop in two phases
    reports_dir.mkdir(parents=True, exist_ok=True)
    review_loop_script = Path(plugin_root) / "scripts" / "review-loop.py"
    code_reviewer_agent = str(Path(plugin_root) / "agents" / "code-reviewer.md")
    func_reviewer_agent = str(Path(plugin_root) / "agents" / "functionality-reviewer.md")

    # Phase 1: Code review
    timestamp_code = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        phase1 = subprocess.run(
            [
                sys.executable,
                str(review_loop_script),
                project_dir,
                commit_sha,
                current_branch,
                str(reports_dir),
                timestamp_code,
                plugin_root,
                code_reviewer_agent,
            ],
            capture_output=True,
            text=True,
        )
        phase1_result = phase1.stdout.strip() if phase1.stdout.strip() else "Code review completed."
        phase1_clean = phase1.returncode == 0
    except Exception:
        phase1_result = "Code review loop failed or timed out."
        phase1_clean = False

    # Phase 2: Functionality review (only if phase 1 succeeded)
    phase2_result = ""
    if phase1_clean:
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        commit_sha_phase2 = head_after.stdout.strip() if head_after.returncode == 0 else commit_sha

        timestamp_func = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            phase2 = subprocess.run(
                [
                    sys.executable,
                    str(review_loop_script),
                    project_dir,
                    commit_sha_phase2,
                    current_branch,
                    str(reports_dir),
                    timestamp_func,
                    plugin_root,
                    func_reviewer_agent,
                    "--skip-scan",
                    "--original-commit",
                    commit_sha,
                ],
                capture_output=True,
                text=True,
            )
            phase2_result = phase2.stdout.strip() if phase2.stdout.strip() else ""
        except Exception:
            phase2_result = "Functionality review loop failed or timed out."

    if phase2_result:
        print(f"[Phase 1 - Code Review] {phase1_result}")
        print(f"[Phase 2 - Functionality Review] {phase2_result}")
    else:
        print(phase1_result)


if __name__ == "__main__":
    main()

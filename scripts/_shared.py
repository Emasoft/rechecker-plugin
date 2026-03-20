#!/usr/bin/env python3
"""Shared utilities for rechecker.py and recheck.py entry points.

Extracts the duplicated lock management and two-phase review orchestration
so both entry points stay DRY while handling their own I/O differently.
"""

import atexit
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def check_and_acquire_lock(project_dir: str) -> tuple[Path, bool]:
    """Check for existing lock, acquire if free.

    Returns (lock_file, acquired).
    If acquired=False, another review is running (caller should exit gracefully).
    """
    lock_dir = Path(project_dir) / ".rechecker"
    lock_file = lock_dir / "rechecker.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            lock_pid = int(lock_file.read_text().strip())
            # Check if the process is still running
            os.kill(lock_pid, 0)
            # Process is alive — another review is running
            return lock_file, False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # Stale lock or unreadable — remove it
            lock_file.unlink(missing_ok=True)

    # Write our PID
    lock_file.write_text(str(os.getpid()))
    return lock_file, True


def setup_lock_cleanup(lock_file: Path) -> None:
    """Register atexit and signal handlers for lock file cleanup."""

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


def is_claude_available() -> bool:
    """Check if the claude CLI is on PATH (cross-platform)."""
    return shutil.which("claude") is not None


def run_two_phase_review(
    project_dir: str,
    commit_sha: str,
    current_branch: str,
    reports_dir: str,
    plugin_root: str,
) -> tuple[str, str]:
    """Run the two-phase review pipeline.

    Phase 1: Code review (syntax, bugs, security) with scan.sh
    Phase 2: Functionality review (does code do what it's supposed to) — only if Phase 1 clean

    Returns (phase1_result, phase2_result). phase2_result is "" if skipped.
    """
    review_loop_script = Path(plugin_root) / "scripts" / "review-loop.py"
    code_reviewer_agent = str(Path(plugin_root) / "agents" / "code-reviewer.md")
    func_reviewer_agent = str(Path(plugin_root) / "agents" / "functionality-reviewer.md")

    Path(reports_dir).mkdir(parents=True, exist_ok=True)

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
                reports_dir,
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

    # Phase 2: Functionality review (only if Phase 1 clean)
    phase2_result = ""
    if phase1_clean:
        # Re-read HEAD since Phase 1 may have merged fix commits
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=project_dir)
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
                    reports_dir,
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

    return phase1_result, phase2_result

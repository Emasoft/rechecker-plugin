#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit commands, acquires lock, invokes review loop.
Outputs JSON with additionalContext for the main Claude session.
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_json_field(data: Any, field_path: str) -> str:
    """Extract a nested field from a dict using dot notation."""
    val: Any = data
    for key in field_path.split("."):
        if isinstance(val, dict):
            val = val.get(key, "")
        else:
            return ""
    return str(val) if val else ""


def is_git_commit(command: str) -> bool:
    """Check if the command contains a real git commit (not --amend).

    Handles compound commands (&&, ;, |).
    Returns True if a git commit is found and --amend is NOT present.
    """
    # If --amend appears ANYWHERE in the full command, reject it entirely.
    # This prevents false positives like 'git commit -m msg; git commit --amend'
    if re.search(r"--amend", command):
        return False

    # Split on && and ; and | to handle compound commands
    parts = re.split(r"&&|;|\|", command)
    for part in parts:
        part = part.strip()
        if re.search(r"\bgit\s+commit\b", part):
            return True

    return False


def output_hook_json(context_msg: str) -> None:
    """Print the PostToolUse hook JSON response."""
    escaped = json.dumps(context_msg)
    # json.dumps adds quotes, strip them for embedding in the template
    escaped_inner = escaped[1:-1]
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"[Rechecker] {escaped_inner}",
                }
            }
        )
    )


def main() -> None:
    # Read hook input from stdin
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    command = parse_json_field(hook_input, "tool_input.command")
    project_dir = parse_json_field(hook_input, "cwd")

    # Fallback to env var if cwd not in JSON
    if not project_dir:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    # Gate: only process Bash tool calls
    if tool_name != "Bash":
        sys.exit(0)

    # Gate: check if command contains a real git commit
    if not is_git_commit(command):
        sys.exit(0)

    # Gate: verify we are in a git repository
    git_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        cwd=project_dir,
    )
    if git_check.returncode != 0:
        sys.exit(0)

    # Gate: verify claude CLI is available
    if not any((Path(d) / "claude").exists() for d in os.environ.get("PATH", "").split(os.pathsep) if d):
        output_hook_json("ERROR: 'claude' CLI not found on PATH. Cannot run automated review.")
        sys.exit(0)

    # Acquire lock
    lock_dir = Path(project_dir) / ".rechecker"
    lock_file = lock_dir / "rechecker.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            lock_pid = int(lock_file.read_text().strip())
            # Check if the process is still running
            os.kill(lock_pid, 0)
            # Process is alive - another review is running
            output_hook_json("Skipped: another review cycle is already in progress.")
            sys.exit(0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # Stale lock or unreadable - remove it
            lock_file.unlink(missing_ok=True)

    # Write our PID
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

    # Get commit info
    head_result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=project_dir)
    commit_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if not commit_sha:
        sys.exit(0)

    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    # Prepare reports directory
    reports_dir = Path(project_dir) / "reports_dev"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Resolve plugin root
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    # Run the review loop
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    review_loop_script = Path(plugin_root) / "scripts" / "review-loop.py"

    try:
        loop_run = subprocess.run(
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
        loop_result = loop_run.stdout.strip() if loop_run.stdout.strip() else "Review completed."
    except Exception:
        loop_result = "Review loop failed or timed out."

    output_hook_json(loop_result)
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit commands, acquires lock, invokes review loop.
Outputs JSON with additionalContext for the main Claude session.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from _shared import check_and_acquire_lock, is_claude_available, run_two_phase_review, setup_lock_cleanup


def parse_json_field(data: Any, field_path: str) -> str:
    """Extract a nested field from a dict using dot notation."""
    val: Any = data
    for key in field_path.split("."):
        if isinstance(val, dict):
            val = val.get(key, "")
        else:
            return ""
    return str(val) if val else ""


def extract_effective_cwd(command: str, default_cwd: str) -> str:
    """Extract the effective working directory from a compound command.

    Handles patterns like:
    - cd /path/to/repo && git commit ...
    - cd /path/to/repo ; git commit ...
    - cd "/path with spaces/repo" && git commit ...
    """
    m = re.match(r'cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))\s*(?:&&|;)', command)
    if m:
        cd_path = m.group(2) or m.group(3) or m.group(4)
        resolved = Path(cd_path).expanduser()
        if not resolved.is_absolute():
            resolved = Path(default_cwd) / resolved
        if resolved.is_dir():
            return str(resolved)
    return default_cwd


def find_git_root(start: str) -> str | None:
    """Walk up from start directory looking for a .git directory."""
    p = Path(start)
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


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
    # Let json.dumps handle all escaping in a single pass
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"[Rechecker] {context_msg}",
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

    # Resolve effective cwd: the Bash command may cd into a subdirectory
    # before running git commit (e.g., "cd my-plugin && git commit ...")
    project_dir = extract_effective_cwd(command, project_dir)

    # Gate: verify we are in a git repository (try cwd first, then walk up)
    git_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        cwd=project_dir,
    )
    if git_check.returncode != 0:
        # cwd might be a parent of the git repo — walk up to find .git
        git_root = find_git_root(project_dir)
        if git_root:
            project_dir = git_root
        else:
            sys.exit(0)

    # Gate: verify claude CLI is available (shutil.which handles Windows .exe/.cmd)
    if not is_claude_available():
        output_hook_json("ERROR: 'claude' CLI not found on PATH. Cannot run automated review.")
        sys.exit(0)

    # Acquire lock
    lock_file, acquired = check_and_acquire_lock(project_dir)
    if not acquired:
        output_hook_json("Skipped: another review cycle is already in progress.")
        sys.exit(0)
    setup_lock_cleanup(lock_file)

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
    reports_dir = str(Path(project_dir) / "reports_dev")

    # Resolve plugin root
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    # Run two-phase review pipeline
    phase1_result, phase2_result = run_two_phase_review(
        project_dir, commit_sha, current_branch, reports_dir, plugin_root
    )

    # Combine results for hook context
    if phase2_result:
        combined = f"[Phase 1 - Code Review] {phase1_result}\n[Phase 2 - Functionality Review] {phase2_result}"
    else:
        combined = phase1_result

    output_hook_json(combined)
    sys.exit(0)


if __name__ == "__main__":
    main()

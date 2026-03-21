#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit commands, acquires lock, invokes review loop.
Supports multiple git repos in a single command (cd /repo1 && git commit && cd /repo2 && git commit).
Outputs JSON with additionalContext for the main Claude session.
"""

import json
import os
import re
import shutil
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


def is_git_commit(command: str) -> bool:
    """Check if the command contains a real git commit (not --amend).

    Handles compound commands (&&, ;, |).
    Returns True if a git commit is found and --amend is NOT present.
    """
    # If --amend appears ANYWHERE in the full command, reject it entirely.
    if re.search(r"--amend", command):
        return False

    parts = re.split(r"&&|;|\|", command)
    for part in parts:
        part = part.strip()
        if re.search(r"\bgit\s+commit\b", part):
            return True

    return False


def extract_git_commit_dirs(command: str, default_cwd: str) -> list[str]:
    """Extract all directories where git commit runs in a compound command.

    Tracks cd commands to follow directory changes, then records the effective
    cwd whenever a git commit is found. Handles:
    - cd /repo1 && git commit -m "msg"
    - cd /repo1 && git commit && cd /repo2 && git commit
    - git commit (uses default_cwd)
    - cd "/path with spaces" && git commit
    """
    parts = re.split(r"&&|;", command)
    current_cwd = default_cwd
    commit_dirs: list[str] = []

    for part in parts:
        part = part.strip()
        # Track cd commands to follow directory changes
        cd_match = re.match(r'cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', part)
        if cd_match:
            cd_path = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
            resolved = Path(cd_path).expanduser()
            if not resolved.is_absolute():
                resolved = Path(current_cwd) / resolved
            if resolved.is_dir():
                current_cwd = str(resolved)
        # Record cwd when git commit is found
        if re.search(r"\bgit\s+commit\b", part):
            commit_dirs.append(current_cwd)

    return commit_dirs


def find_git_root(start: str) -> str | None:
    """Walk up from start directory looking for a .git directory."""
    p = Path(start)
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def check_git_lfs(git_root: str) -> str | None:
    """Check if repo needs git-lfs but it's not installed. Returns warning or None."""
    gitattrs = Path(git_root) / ".gitattributes"
    if not gitattrs.is_file():
        return None
    try:
        content = gitattrs.read_text()
    except OSError:
        return None
    if "filter=lfs" not in content:
        return None
    # Repo uses LFS — check if git-lfs is installed
    if not shutil.which("git-lfs"):
        return (
            f"WARNING: {git_root} uses git-lfs but git-lfs is not installed. Large files may not be tracked correctly."
        )
    # Check if LFS is configured in this repo
    r = subprocess.run(["git", "lfs", "env"], capture_output=True, text=True, cwd=git_root)
    if r.returncode != 0:
        return f"WARNING: {git_root} uses git-lfs but it may not be configured. Run: git lfs install"
    return None


def output_hook_json(context_msg: str) -> None:
    """Print the PostToolUse hook JSON response."""
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


def review_single_repo(git_root: str, plugin_root: str) -> str:
    """Run two-phase review on a single git repo. Returns result string."""
    head_result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=git_root)
    commit_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if not commit_sha:
        return f"[{git_root}] No HEAD commit found."

    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    reports_dir = str(Path(git_root) / "reports_dev")

    phase1_result, phase2_result = run_two_phase_review(git_root, commit_sha, current_branch, reports_dir, plugin_root)

    if phase2_result:
        return f"[Phase 1 - Code Review] {phase1_result}\n[Phase 2 - Functionality Review] {phase2_result}"
    return phase1_result


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

    # Gate: verify claude CLI is available
    if not is_claude_available():
        output_hook_json("ERROR: 'claude' CLI not found on PATH. Cannot run automated review.")
        sys.exit(0)

    # Extract all directories where git commit runs in this command
    commit_dirs = extract_git_commit_dirs(command, project_dir)
    if not commit_dirs:
        commit_dirs = [project_dir]

    # Resolve each commit dir to its git root, deduplicate
    git_roots: list[str] = []
    seen_roots: set[str] = set()
    for d in commit_dirs:
        root = find_git_root(d)
        if root and root not in seen_roots:
            git_roots.append(root)
            seen_roots.add(root)

    # No git repo found anywhere
    if not git_roots:
        output_hook_json(
            "WARNING: No git repository found. The commit command ran but rechecker "
            "could not locate a .git directory at or above: " + ", ".join(commit_dirs)
        )
        sys.exit(0)

    # Resolve plugin root
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    # Check git-lfs warnings for each repo
    warnings: list[str] = []
    for root in git_roots:
        lfs_warning = check_git_lfs(root)
        if lfs_warning:
            warnings.append(lfs_warning)

    # Acquire locks for all repos, track which we acquired
    acquired_locks: list[Path] = []
    skipped_repos: list[str] = []
    reviewable_roots: list[str] = []

    for root in git_roots:
        lock_file, acquired = check_and_acquire_lock(root)
        if acquired:
            setup_lock_cleanup(lock_file)
            acquired_locks.append(lock_file)
            reviewable_roots.append(root)
        else:
            skipped_repos.append(root)

    if not reviewable_roots:
        msg = "Skipped: all repos already have reviews in progress."
        if warnings:
            msg = "\n".join(warnings) + "\n" + msg
        output_hook_json(msg)
        sys.exit(0)

    # Run reviews for each repo
    results: list[str] = []
    for root in reviewable_roots:
        if len(reviewable_roots) > 1:
            # Prefix with repo path when reviewing multiple repos
            result = f"[Repo: {root}]\n{review_single_repo(root, plugin_root)}"
        else:
            result = review_single_repo(root, plugin_root)
        results.append(result)

    # Combine all results
    combined_parts: list[str] = []
    if warnings:
        combined_parts.extend(warnings)
    if skipped_repos:
        combined_parts.append(f"Skipped (already reviewing): {', '.join(skipped_repos)}")
    combined_parts.extend(results)

    output_hook_json("\n".join(combined_parts))
    sys.exit(0)


if __name__ == "__main__":
    main()

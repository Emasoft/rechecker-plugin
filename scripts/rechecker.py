#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit commands, forks review to background (non-blocking),
and returns immediately with a context message.
Supports multiple git repos in a single compound command.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from _shared import check_and_acquire_lock, is_claude_available


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
    """Check if the command contains a real git commit (not --amend)."""
    if re.search(r"--amend", command):
        return False
    parts = re.split(r"&&|;|\|", command)
    for part in parts:
        part = part.strip()
        if re.search(r"\bgit\s+commit\b", part):
            return True
    return False


def extract_git_commit_dirs(command: str, default_cwd: str) -> list[str]:
    """Extract all directories where git commit runs in a compound command."""
    parts = re.split(r"&&|;", command)
    current_cwd = default_cwd
    commit_dirs: list[str] = []

    for part in parts:
        part = part.strip()
        cd_match = re.match(r'cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', part)
        if cd_match:
            cd_path = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
            resolved = Path(cd_path).expanduser()
            if not resolved.is_absolute():
                resolved = Path(current_cwd) / resolved
            if resolved.is_dir():
                current_cwd = str(resolved)
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
    if not shutil.which("git-lfs"):
        return (
            f"WARNING: {git_root} uses git-lfs but git-lfs is not installed. Large files may not be tracked correctly."
        )
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


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    command = parse_json_field(hook_input, "tool_input.command")
    project_dir = parse_json_field(hook_input, "cwd")

    if not project_dir:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    if tool_name != "Bash":
        sys.exit(0)

    if not is_git_commit(command):
        sys.exit(0)

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

    if not git_roots:
        output_hook_json(
            "WARNING: No git repository found. The commit command ran but rechecker "
            "could not locate a .git directory at or above: " + ", ".join(commit_dirs)
        )
        sys.exit(0)

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))
    bg_script = str(Path(plugin_root) / "scripts" / "_background_review.py")

    # Check git-lfs warnings
    warnings: list[str] = []
    for root in git_roots:
        lfs_warning = check_git_lfs(root)
        if lfs_warning:
            warnings.append(lfs_warning)

    # Try to acquire locks and launch background reviews
    launched: list[str] = []
    skipped: list[str] = []

    for root in git_roots:
        lock_file, acquired = check_and_acquire_lock(root)
        if not acquired:
            skipped.append(root)
            continue

        # Lock acquired — fork background review process (non-blocking)
        # The background process takes over the lock and cleans it up when done
        rechecker_dir = Path(root) / ".rechecker"
        rechecker_dir.mkdir(parents=True, exist_ok=True)
        log_path = rechecker_dir / "background.log"

        with open(log_path, "w") as log_f:
            subprocess.Popen(
                [sys.executable, bg_script, root, plugin_root],
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=root,
            )

        launched.append(root)

    # Build response message (returned immediately, review runs in background)
    parts: list[str] = []
    if warnings:
        parts.extend(warnings)
    if launched:
        repos = ", ".join(Path(r).name for r in launched)
        parts.append(
            f"Review started in background for: {repos}. "
            "Reports will be saved to reports_dev/ when complete. "
            "Use /recheck to check status or re-run manually."
        )
    if skipped:
        repos = ", ".join(Path(r).name for r in skipped)
        parts.append(f"Skipped (already reviewing): {repos}")
    if not launched and not skipped:
        parts.append("No repos to review.")

    output_hook_json("\n".join(parts))
    sys.exit(0)


if __name__ == "__main__":
    main()

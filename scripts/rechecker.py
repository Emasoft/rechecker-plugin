#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit in Bash commands, finds all git repos involved,
launches claude --worktree --agent for each. Runs async (hooks.json).
"""

import json
import os
import re
import shutil
import subprocess
import sys
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
                resolved = Path(default_cwd) / resolved
            if resolved.is_dir():
                current_cwd = str(resolved)
        if re.search(r"\bgit\s+commit\b", part):
            commit_dirs.append(current_cwd)
    return commit_dirs


def find_git_root(start: str) -> str | None:
    """Walk up from start looking for .git directory."""
    p = Path(start)
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            return str(parent)
    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    command = parse_json_field(hook_input, "tool_input.command")
    cwd = parse_json_field(hook_input, "cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    # Gate: only Bash with git commit (not --amend)
    if tool_name != "Bash":
        sys.exit(0)
    if re.search(r"--amend", command):
        sys.exit(0)
    if not any(re.search(r"\bgit\s+commit\b", p) for p in re.split(r"&&|;|\|", command)):
        sys.exit(0)

    # Gate: verify claude CLI is available
    if not shutil.which("claude"):
        sys.exit(0)

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))
    orchestrator = str(Path(plugin_root) / "agents" / "rechecker-orchestrator.md")

    # Find all git roots where commits happened
    commit_dirs = extract_git_commit_dirs(command, cwd) or [cwd]
    git_roots: list[str] = []
    seen: set[str] = set()
    for d in commit_dirs:
        root = find_git_root(d)
        if root and root not in seen:
            git_roots.append(root)
            seen.add(root)

    if not git_roots:
        sys.exit(0)

    # Launch the orchestrator in a named worktree for each git root.
    # The orchestrator runs all 4 loops (lint→code review→func review→final lint),
    # makes ONE commit, then exits. Claude Code merges the worktree.
    for root in git_roots:
        wt_name = f"rechecker-{Path(root).name}"
        subprocess.Popen(
            ["claude", "--worktree", wt_name, "--agent", orchestrator, "--dangerously-skip-permissions"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


if __name__ == "__main__":
    main()

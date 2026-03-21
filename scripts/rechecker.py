#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit in Bash commands, finds the git root where each
commit ran, launches claude --worktree --agent for each.
Runs async (hooks.json "async": true).

KNOWN ISSUE: PostToolUse may not fire for the git commit Bash call
when a PreToolUse hook (e.g., git_safety_guard) is also registered
for Bash. This is a Claude Code bug. Use /recheck as a workaround.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_LINES: list[str] = []


def _log(msg: str) -> None:
    _LOG_LINES.append(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def _flush_log(project_dir: str) -> None:
    if not _LOG_LINES:
        return
    try:
        log_dir = Path(project_dir) / "reports_dev"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "rechecker_hook.log", "a") as f:
            f.write("\n".join(_LOG_LINES) + "\n")
    except OSError:
        pass


def parse_json_field(data: Any, field_path: str) -> str:
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
                _log(f"  cd -> {current_cwd}")
        if re.search(r"\bgit\s+commit\b", part):
            commit_dirs.append(current_cwd)
            _log(f"  git commit in: {current_cwd}")
    return commit_dirs


def find_git_root(start: str) -> str | None:
    """Find the git root. Handles normal repos, submodules (.git file), nested subdirs."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    p = Path(start)
    for parent in [p, *p.parents]:
        git_path = parent / ".git"
        if git_path.is_dir() or git_path.is_file():
            return str(parent)
    return None


def main() -> None:
    raw = sys.stdin.read()
    # Log immediately — before any gates — so we know the hook fired
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    _log(f"--- PostToolUse fired ({len(raw)} bytes) ---")

    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _log(f"JSON parse error: {e}")
        _flush_log(project_dir)
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    command = parse_json_field(hook_input, "tool_input.command")
    cwd = (
        parse_json_field(hook_input, "tool_input.cwd")
        or parse_json_field(hook_input, "cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.getcwd()
    )

    _log(f"tool={tool_name} cwd={cwd} command={command[:200]}")
    if isinstance(hook_input, dict):
        _log(f"hook keys: {sorted(hook_input.keys())}")

    # Gate: only Bash with git commit (not --amend)
    if tool_name != "Bash":
        _log("skip: not Bash")
        _flush_log(project_dir)
        sys.exit(0)
    if re.search(r"--amend", command):
        _log("skip: --amend")
        _flush_log(project_dir)
        sys.exit(0)
    if not any(re.search(r"\bgit\s+commit\b", p) for p in re.split(r"&&|;|\|", command)):
        _log("skip: no git commit in command")
        _flush_log(project_dir)
        sys.exit(0)
    if not shutil.which("claude"):
        _log("skip: claude not on PATH")
        _flush_log(project_dir)
        sys.exit(0)

    _log("PASSED all gates — git commit detected")

    # Find all git roots where commits happened
    commit_dirs = extract_git_commit_dirs(command, cwd) or [cwd]
    _log(f"commit_dirs={commit_dirs}")

    git_roots: list[str] = []
    seen: set[str] = set()
    for d in commit_dirs:
        root = find_git_root(d)
        _log(f"  find_git_root({d}) -> {root}")
        if root and root not in seen:
            git_roots.append(root)
            seen.add(root)

    if not git_roots:
        _log("no git roots found")
        _flush_log(cwd)
        sys.exit(0)

    _log(f"launching orchestrator for {len(git_roots)} repo(s): {git_roots}")
    # Flush log NOW so the "launching" entry is visible immediately
    # (otherwise it only appears after the orchestrator finishes, which can take 30+ min)
    _flush_log(cwd)

    orchestrator = "rechecker-plugin:rechecker-orchestrator"

    for root in git_roots:
        wt_name = f"rechecker-{uuid.uuid4().hex[:8]}"
        cmd = [
            "claude", "--worktree", wt_name,
            "--agent", orchestrator,
            "--dangerously-skip-permissions",
            "-p", "Run the full recheck pipeline on the latest commit.",
        ]
        _log(f"  cmd: {' '.join(cmd)}")
        _log(f"  cwd: {root}")
        _flush_log(cwd)

        reports_dev = Path(root) / "reports_dev"
        reports_dev.mkdir(parents=True, exist_ok=True)
        stderr_log = reports_dev / "rechecker_claude_stderr.log"
        with open(stderr_log, "a") as stderr_f:
            result = subprocess.run(cmd, cwd=root, stdout=subprocess.DEVNULL, stderr=stderr_f)
        _log(f"  claude exit code: {result.returncode}")

        for report in Path(root).glob("rechecker-report-*.md"):
            report.rename(reports_dev / report.name)
            _log(f"  moved report: {report.name}")

    _flush_log(cwd)


if __name__ == "__main__":
    main()

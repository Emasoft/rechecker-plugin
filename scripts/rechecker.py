#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects new commits by tracking HEAD SHA, not by parsing command text.
This handles the case where PostToolUse doesn't fire for the git commit
Bash call (e.g., due to PreToolUse hook interactions). The next Bash
call that DOES fire will catch the new commit.

Runs async (hooks.json "async": true).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Diagnostic log — written to {project}/reports_dev/rechecker_hook.log
_LOG_LINES: list[str] = []


def _log(msg: str) -> None:
    """Buffer a timestamped log line (flushed at exit)."""
    _LOG_LINES.append(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def _flush_log(project_dir: str) -> None:
    """Write buffered log lines to reports_dev/rechecker_hook.log."""
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
    """Extract a nested field from a dict using dot notation."""
    val: Any = data
    for key in field_path.split("."):
        if isinstance(val, dict):
            val = val.get(key, "")
        else:
            return ""
    return str(val) if val else ""


def find_git_root(start: str) -> str | None:
    """Find the git root for start dir. Handles normal repos, submodules (.git file), and nested subdirs."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Fallback: walk up looking for .git (directory or file)
    p = Path(start)
    for parent in [p, *p.parents]:
        git_path = parent / ".git"
        if git_path.is_dir() or git_path.is_file():
            return str(parent)
    return None


def get_head_sha(git_root: str) -> str | None:
    """Get the current HEAD SHA for a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def get_commit_message(git_root: str) -> str | None:
    """Get the commit message of HEAD."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", "HEAD"],
            cwd=git_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def discover_git_roots(cwd: str, command: str) -> list[str]:
    """Find all git repos to check: cwd, cd targets from command, and direct children of cwd."""
    roots: dict[str, None] = {}  # ordered set

    # 1. cwd itself
    root = find_git_root(cwd)
    if root:
        roots[root] = None

    # 2. Parse cd targets from the command (handles compound commands)
    current = cwd
    for part in re.split(r"&&|;", command):
        part = part.strip()
        cd_match = re.match(r'cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', part)
        if cd_match:
            cd_path = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
            resolved = Path(cd_path).expanduser()
            if not resolved.is_absolute():
                resolved = Path(current) / resolved
            if resolved.is_dir():
                current = str(resolved)
                r = find_git_root(current)
                if r:
                    roots[r] = None

    # 3. If cwd is NOT a git repo, scan direct children (monorepo with sub-repos)
    if not root:
        try:
            for child in sorted(Path(cwd).iterdir())[:20]:
                if child.is_dir() and not child.name.startswith("."):
                    r = find_git_root(str(child))
                    if r and r not in roots:
                        roots[r] = None
        except OSError:
            pass

    return list(roots)


def check_new_commit(git_root: str) -> bool:
    """Check if git_root has a new, unreviewed commit. Updates state file."""
    state_file = Path(git_root) / ".rechecker" / "last_sha"

    head = get_head_sha(git_root)
    if not head:
        return False

    # Compare to last reviewed SHA
    try:
        last = state_file.read_text().strip()
    except FileNotFoundError:
        last = ""

    if head == last:
        return False

    # Skip our own commits
    msg = get_commit_message(git_root)
    if msg and msg.startswith("rechecker:"):
        _log(f"  skip rechecker's own commit: {head[:8]}")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(head)
        return False

    # New commit found — mark as reviewed (before launching, to prevent double-launch)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(head)
    return True


def main() -> None:
    raw = sys.stdin.read()

    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    command = parse_json_field(hook_input, "tool_input.command")
    cwd = (
        parse_json_field(hook_input, "tool_input.cwd")
        or parse_json_field(hook_input, "cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.getcwd()
    )

    # Gate: only Bash tool
    if tool_name != "Bash":
        sys.exit(0)

    # Gate: skip --amend commands
    if re.search(r"--amend", command):
        sys.exit(0)

    # Gate: verify claude CLI is available
    if not shutil.which("claude"):
        sys.exit(0)

    _log("--- hook fired ---")
    _log(f"cwd={cwd}")
    _log(f"command={command[:200]}")

    # Log hook JSON keys on first run (helps debug schema)
    if isinstance(hook_input, dict):
        _log(f"hook keys: {sorted(hook_input.keys())}")

    # Discover all git repos reachable from cwd and command
    git_roots = discover_git_roots(cwd, command)
    if not git_roots:
        _log("no git roots found")
        _flush_log(cwd)
        sys.exit(0)

    _log(f"git roots: {git_roots}")

    # Check each repo for new unreviewed commits
    roots_to_review: list[str] = []
    for root in git_roots:
        head = get_head_sha(root)
        _log(f"  {root}: HEAD={head[:8] if head else 'None'}")
        if check_new_commit(root):
            _log("  -> NEW COMMIT, will review")
            roots_to_review.append(root)
        else:
            _log("  -> already reviewed or no commit")

    if not roots_to_review:
        _flush_log(cwd)
        sys.exit(0)

    _log(f"launching orchestrator for {len(roots_to_review)} repo(s)")

    orchestrator = "rechecker-plugin:rechecker-orchestrator"

    for root in roots_to_review:
        wt_name = f"rechecker-{Path(root).name}"
        cmd = [
            "claude", "--worktree", wt_name,
            "--agent", orchestrator,
            "--dangerously-skip-permissions",
            "-p", "Run the full recheck pipeline on the latest commit.",
        ]
        _log(f"  cmd: {' '.join(cmd)}")
        _log(f"  cwd: {root}")

        # Capture stderr for diagnostics, discard stdout
        reports_dev = Path(root) / "reports_dev"
        reports_dev.mkdir(parents=True, exist_ok=True)
        stderr_log = reports_dev / "rechecker_claude_stderr.log"
        with open(stderr_log, "a") as stderr_f:
            result = subprocess.run(cmd, cwd=root, stdout=subprocess.DEVNULL, stderr=stderr_f)
        _log(f"  claude exit code: {result.returncode}")

        # After worktree merge: move report to reports_dev/ (gitignored)
        for report in Path(root).glob("rechecker-report-*.md"):
            report.rename(reports_dev / report.name)
            _log(f"  moved report: {report.name}")

    _flush_log(cwd)


if __name__ == "__main__":
    main()

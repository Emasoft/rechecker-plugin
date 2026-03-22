#!/usr/bin/env python3
"""SessionStart[resume] hook handler — detect pending rechecker merges and incomplete runs.

Fires when Claude Code session resumes (e.g. after rate limit pause).
Checks the project directory for:
1. rck-*-merge-pending.md files → fixes ready to merge
2. Incomplete worktree runs → re-launch rechecker in existing worktree

Outputs additionalContext as JSON so Claude sees the pending work.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _get_project_dir(hook_input: dict) -> str:
    """Extract project directory from hook input or environment."""
    return (
        hook_input.get("cwd", "")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.getcwd()
    )


def _find_merge_pending(project_dir: str) -> list[dict]:
    """Find rck-*-merge-pending.md files in the project root."""
    results = []
    root = Path(project_dir)
    for f in sorted(root.glob("rck-*-merge-pending.md")):
        # Extract worktree name and branch from the file content
        content = f.read_text()
        branch_match = re.search(r"Branch with fixes[^`]*`([^`]+)`", content)
        wt_match = re.search(r"Worktree[^`]*`([^`]+)`", content)
        report_match = re.search(r"Report[^`]*`([^`]+)`", content)
        branch = branch_match.group(1) if branch_match else None
        wt_name = wt_match.group(1) if wt_match else None

        # Verify the branch still exists
        if branch:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=project_dir, capture_output=True, text=True, timeout=5,
            )
            branch_exists = result.returncode == 0
        else:
            branch_exists = False

        # Check age of the file
        try:
            age_hours = (Path(f).stat().st_mtime - __import__("time").time()) / -3600
        except OSError:
            age_hours = 0

        results.append({
            "file": f.name,
            "branch": branch,
            "worktree": wt_name,
            "report": report_match.group(1) if report_match else None,
            "branch_exists": branch_exists,
            "age_hours": round(age_hours, 1),
        })
    return results


def _find_incomplete_worktrees(project_dir: str) -> list[dict]:
    """Find rechecker worktrees that started but never completed."""
    results = []

    # List all worktrees
    try:
        wt_output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return results

    # Parse worktree list
    worktrees = []
    current_wt: dict = {}
    for line in wt_output.stdout.splitlines():
        if line.startswith("worktree "):
            if current_wt:
                worktrees.append(current_wt)
            current_wt = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            current_wt["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
    if current_wt:
        worktrees.append(current_wt)

    for wt in worktrees:
        branch = wt.get("branch", "")
        # Only check rechecker worktrees (rck-XXXXXX naming)
        if not branch.startswith("worktree-rck-"):
            continue

        wt_path = Path(wt["path"])
        progress_file = wt_path / ".rechecker" / "rck-progress.json"
        index_file = wt_path / ".rechecker" / "index.json"
        # Check for final report in worktree root
        has_final_report = any(wt_path.glob("rck-*-report.md"))

        if not index_file.exists():
            # Worktree exists but pipeline never even initialized — stale
            continue

        if has_final_report:
            # Pipeline completed (final report exists) but merge-pending wasn't picked up
            # This is handled by _find_merge_pending, not here
            continue

        # No final report — pipeline was interrupted
        wt_name = branch.replace("worktree-", "")
        progress = {}
        if progress_file.exists():
            try:
                progress = json.loads(progress_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Check if a Claude process is still running in this worktree
        is_running = False
        try:
            ps_output = subprocess.run(
                ["pgrep", "-f", wt_name],
                capture_output=True, text=True, timeout=5,
            )
            # Filter for actual claude processes, not just grep matches
            for pid in ps_output.stdout.strip().splitlines():
                try:
                    cmd_result = subprocess.run(
                        ["ps", "-p", pid, "-o", "command="],
                        capture_output=True, text=True, timeout=5,
                    )
                    if "claude" in cmd_result.stdout.lower():
                        is_running = True
                        break
                except (subprocess.TimeoutExpired, OSError):
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        if is_running:
            # Claude is still running in this worktree — don't interfere
            continue

        results.append({
            "worktree": wt_name,
            "branch": branch,
            "path": str(wt_path),
            "status": progress.get("status", "unknown"),
            "current_loop": progress.get("current_loop"),
            "current_iter": progress.get("current_iter"),
        })

    return results


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    project_dir = _get_project_dir(hook_input)
    if not project_dir or not Path(project_dir).is_dir():
        sys.exit(0)

    # Check if this is even a git repo
    git_dir = Path(project_dir) / ".git"
    if not git_dir.exists():
        sys.exit(0)

    merge_pending = _find_merge_pending(project_dir)
    incomplete = _find_incomplete_worktrees(project_dir)

    if not merge_pending and not incomplete:
        sys.exit(0)

    # Build the context message for Claude
    lines = ["## Rechecker: Pending Work Detected on Resume", ""]

    if merge_pending:
        valid = [m for m in merge_pending if m["branch_exists"]]
        stale = [m for m in merge_pending if not m["branch_exists"]]

        if valid:
            lines.append(f"### {len(valid)} merge(s) ready")
            lines.append("")
            lines.append("The rechecker reviewed your commits and produced fixes.")
            lines.append("Merge all at once:")
            lines.append("")
            lines.append("```bash")
            lines.append(f'cd "{project_dir}" && bash .rechecker/merge-worktrees.sh')
            lines.append("```")
            lines.append("")
            lines.append("Or merge individually:")
            lines.append("")
            for m in valid:
                lines.append(f"```bash")
                lines.append(f'cd "{project_dir}" && git merge {m["branch"]} --no-edit')
                lines.append(f"```")
                if m.get("report"):
                    lines.append(f"Report: `{m['report']}`")
                lines.append("")
            lines.append("After merging all, clean up:")
            lines.append("```bash")
            for m in valid:
                lines.append(f'rm "{project_dir}/{m["file"]}"')
                lines.append(f'git worktree remove "{project_dir}/.claude/worktrees/{m["worktree"]}" 2>/dev/null')
                lines.append(f"git branch -d {m['branch']} 2>/dev/null")
            lines.append("```")
            lines.append("")

        if stale:
            lines.append(f"### {len(stale)} stale merge-pending file(s)")
            lines.append("")
            lines.append("These merge-pending files reference branches that no longer exist. Clean them up:")
            lines.append("```bash")
            for m in stale:
                lines.append(f'rm "{project_dir}/{m["file"]}"')
            lines.append("```")
            lines.append("")

    if incomplete:
        lines.append(f"### {len(incomplete)} interrupted rechecker run(s)")
        lines.append("")
        lines.append("These rechecker worktrees were interrupted mid-pipeline. Re-launch them:")
        lines.append("")
        agent_ref = "rechecker-plugin:rechecker-orchestrator"
        for inc in incomplete:
            loop_info = ""
            if inc.get("current_loop"):
                loop_info = f" (was on loop {inc['current_loop']}, iter {inc.get('current_iter', '?')})"
            lines.append(f"- **{inc['worktree']}**{loop_info}")
            lines.append(f"  ```bash")
            lines.append(f'  claude --worktree {inc["worktree"]} --agent {agent_ref} --dangerously-skip-permissions -p "Resume interrupted recheck. Read .rechecker/rck-progress.json for state."')
            lines.append(f"  ```")
            lines.append("")

    context_message = "\n".join(lines)

    # Output JSON with additionalContext for Claude to see
    output = {
        "hookSpecificOutput": {
            "additionalContext": context_message,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()

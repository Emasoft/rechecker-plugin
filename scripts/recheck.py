#!/usr/bin/env python3
"""recheck.py - On-demand review trigger for /recheck skill.

Without args: scans for git repos with recent commits (last 24h), reviews each.
With commit SHA: reviews that specific commit in the current repo.
Launches claude --worktree --agent for each phase, same as the hook.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_all_git_roots(start: str) -> list[str]:
    """Find all git repos at or under start directory (max 3 levels deep)."""
    roots: set[str] = set()
    p = Path(start)
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            roots.add(str(parent))
            break
    start_path = Path(start)
    for depth in range(4):
        pattern = "/".join(["*"] * depth) + "/.git" if depth > 0 else ".git"
        for git_dir in start_path.glob(pattern):
            if git_dir.is_dir():
                roots.add(str(git_dir.parent))
    return sorted(roots)


def has_recent_commits(git_root: str, max_age_hours: int = 24) -> str | None:
    """Return latest commit SHA if repo has commits in last max_age_hours, else None."""
    r = subprocess.run(
        ["git", "log", "-1", "--format=%H", f"--since={max_age_hours} hours ago"],
        capture_output=True, text=True, cwd=git_root,
    )
    sha = r.stdout.strip()
    return sha if sha else None


def review_repo(git_root: str, plugin_root: str) -> None:
    """Launch the orchestrator in a named worktree. It runs all 4 loops internally."""
    orchestrator = str(Path(plugin_root) / "agents" / "recheck-orchestrator.md")
    wt_name = f"rechecker-{Path(git_root).name}"

    subprocess.run(
        ["claude", "--worktree", wt_name, "--agent", orchestrator, "--dangerously-skip-permissions"],
        cwd=git_root,
    )


def main() -> None:
    commit_sha = sys.argv[1] if len(sys.argv) > 1 else ""
    project_dir = os.getcwd()

    if not shutil.which("claude"):
        print("ERROR: 'claude' CLI not found on PATH.", file=sys.stderr)
        sys.exit(1)

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    if commit_sha:
        # Specific commit mode
        cat_result = subprocess.run(["git", "cat-file", "-t", commit_sha], capture_output=True)
        if cat_result.returncode != 0:
            print(f"ERROR: Commit not found: {commit_sha}", file=sys.stderr)
            sys.exit(1)
        print(f"Reviewing commit {commit_sha[:8]}...")
        review_repo(project_dir, plugin_root)
    else:
        # Discovery mode — all git repos with recent commits
        print("Scanning for git repos with recent commits (last 24h)...")
        all_roots = find_all_git_roots(project_dir)
        if not all_roots:
            print("No git repositories found.", file=sys.stderr)
            sys.exit(1)

        repos: list[str] = []
        for root in all_roots:
            sha = has_recent_commits(root)
            if sha:
                repos.append(root)
                print(f"  Found: {root} (latest: {sha[:8]})")
            else:
                print(f"  Skip:  {root} (no commits in last 24h)")

        if not repos:
            print("No repos with recent commits. Nothing to review.")
            sys.exit(0)

        for root in repos:
            print(f"\nReviewing: {root}")
            review_repo(root, plugin_root)

        print(f"\nDone. Reviewed {len(repos)} repo(s).")


if __name__ == "__main__":
    main()

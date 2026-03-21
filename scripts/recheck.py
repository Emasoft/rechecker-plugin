#!/usr/bin/env python3
"""recheck.py - On-demand review loop trigger.

When called with a commit SHA: reviews that specific commit in the current repo.
When called without args: scans for all git repos under cwd (and parents) that
have commits in the last 24 hours, and reviews each one.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from _shared import check_and_acquire_lock, is_claude_available, run_two_phase_review, setup_lock_cleanup


def find_all_git_roots(start: str) -> list[str]:
    """Find all git repos at or under start directory.

    Walks the directory tree looking for .git directories.
    Also checks parent directories (the start dir might be inside a repo).
    Returns deduplicated list of git root paths.
    """
    roots: set[str] = set()

    # Check parents first (start might be inside a repo)
    p = Path(start)
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            roots.add(str(parent))
            break

    # Walk subdirectories (max 3 levels deep to avoid crawling huge trees)
    start_path = Path(start)
    for depth in range(4):
        pattern = "/".join(["*"] * depth) + "/.git" if depth > 0 else ".git"
        for git_dir in start_path.glob(pattern):
            if git_dir.is_dir():
                roots.add(str(git_dir.parent))

    return sorted(roots)


def has_recent_commits(git_root: str, max_age_hours: int = 24) -> str | None:
    """Check if repo has commits in the last max_age_hours. Returns latest SHA or None."""
    since = f"--since={max_age_hours} hours ago"
    r = subprocess.run(
        ["git", "log", "-1", "--format=%H", since],
        capture_output=True,
        text=True,
        cwd=git_root,
    )
    sha = r.stdout.strip()
    return sha if sha else None


def main() -> None:
    commit_sha = sys.argv[1] if len(sys.argv) > 1 else ""
    project_dir = os.getcwd()

    # Verify claude CLI is available
    if not is_claude_available():
        print("ERROR: 'claude' CLI not found on PATH. Cannot run automated review.", file=sys.stderr)
        sys.exit(1)

    if commit_sha:
        # Specific commit mode — review one commit in the current repo
        cat_result = subprocess.run(["git", "cat-file", "-t", commit_sha], capture_output=True)
        if cat_result.returncode != 0:
            print(f"ERROR: Commit not found: {commit_sha}", file=sys.stderr)
            sys.exit(1)

        branch_result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

        reports_dir = str(Path(project_dir) / "reports_dev")
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

        lock_file, acquired = check_and_acquire_lock(project_dir)
        if not acquired:
            print("Another review cycle is already in progress. Skipping.")
            sys.exit(0)
        setup_lock_cleanup(lock_file)

        phase1_result, phase2_result = run_two_phase_review(
            project_dir, commit_sha, current_branch, reports_dir, plugin_root
        )

        if phase2_result:
            print(f"[Phase 1 - Code Review] {phase1_result}")
            print(f"[Phase 2 - Functionality Review] {phase2_result}")
        else:
            print(phase1_result)
    else:
        # Discovery mode — scan for all git repos with recent commits (last 24h)
        print("Scanning for git repos with recent commits (last 24h)...")
        all_roots = find_all_git_roots(project_dir)

        if not all_roots:
            print("No git repositories found.", file=sys.stderr)
            sys.exit(1)

        # Filter to repos with recent commits
        repos_to_review: list[tuple[str, str]] = []
        for root in all_roots:
            recent_sha = has_recent_commits(root)
            if recent_sha:
                repos_to_review.append((root, recent_sha))
                print(f"  Found: {root} (latest: {recent_sha[:8]})")
            else:
                print(f"  Skip:  {root} (no commits in last 24h)")

        if not repos_to_review:
            print("No repos with recent commits found. Nothing to review.")
            sys.exit(0)

        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

        # Review each repo
        for root, sha in repos_to_review:
            print(f"\n{'=' * 60}")
            print(f"Reviewing: {root} (commit {sha[:8]})")
            print(f"{'=' * 60}")

            lock_file, acquired = check_and_acquire_lock(root)
            if not acquired:
                print(f"  Skipped (another review in progress for {root})")
                continue
            setup_lock_cleanup(lock_file)

            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=root,
            )
            current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"
            reports_dir = str(Path(root) / "reports_dev")

            phase1_result, phase2_result = run_two_phase_review(root, sha, current_branch, reports_dir, plugin_root)

            if phase2_result:
                print(f"[Phase 1] {phase1_result}")
                print(f"[Phase 2] {phase2_result}")
            else:
                print(phase1_result)

            # Small delay between repos to avoid rate limiting
            if len(repos_to_review) > 1:
                time.sleep(2)

        print(f"\nDone. Reviewed {len(repos_to_review)} repo(s).")


if __name__ == "__main__":
    main()

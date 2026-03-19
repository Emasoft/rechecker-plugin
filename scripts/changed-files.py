#!/usr/bin/env python3
"""Generate a list of changed files from a git commit.

Outputs one file path per line (relative to repo root).
Designed to pipe into scan.sh --target-list or save to a file.

Usage:
    changed-files.py <commit_sha> [output_file]

Handles:
    - First commit in repo (no parent): uses git show --name-only
    - Merge commits: shows combined diff (files changed from any parent)
    - Deleted files: excluded (they don't exist on disk to scan)
    - Binary files: included (linters can flag them)
    - Empty result: exits 0 with no output (caller should check)
"""

import subprocess
import sys


def run_git(*args: str) -> tuple[str, int]:
    """Run a git command, return (stdout, returncode)."""
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def main() -> None:
    commit_sha = sys.argv[1] if len(sys.argv) > 1 else ""
    output_file = sys.argv[2] if len(sys.argv) > 2 else ""

    if not commit_sha:
        print("Usage: changed-files.py <commit_sha> [output_file]", file=sys.stderr)
        sys.exit(1)

    # Verify we are in a git repo
    _, rc = run_git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        print("ERROR: not inside a git repository", file=sys.stderr)
        sys.exit(1)

    # Verify the commit exists
    _, rc = run_git("cat-file", "-t", commit_sha)
    if rc != 0:
        print(f"ERROR: commit not found: {commit_sha}", file=sys.stderr)
        sys.exit(1)

    # Generate the list of changed files using git show (works uniformly for
    # normal commits, first commits, and merge commits).
    # --diff-filter=d excludes deleted files (they don't exist on disk to scan).
    out, rc = run_git("show", "--name-only", "--format=", "--diff-filter=d", commit_sha)
    if rc != 0:
        out = ""

    # Filter out empty lines
    lines = [line for line in out.splitlines() if line.strip()]
    result = "\n".join(lines)

    if not result:
        # No changed files - not an error, just nothing to scan
        if output_file:
            open(output_file, "w").close()
        sys.exit(0)

    # Output
    if output_file:
        with open(output_file, "w") as f:
            f.write(result + "\n")
    else:
        print(result)


if __name__ == "__main__":
    main()

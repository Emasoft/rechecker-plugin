#!/usr/bin/env bash
# changed-files.sh — Generate a list of changed files from a git commit
#
# Outputs one file path per line (relative to repo root) to stdout.
# Designed to pipe into scan.sh --target-list or save to a file.
#
# Usage:
#   changed-files.sh <commit_sha> [output_file]
#
#   commit_sha    The commit to diff against its parent
#   output_file   Optional: write to file instead of stdout
#
# Examples:
#   changed-files.sh abc123                          # print to stdout
#   changed-files.sh abc123 .rechecker_changed.txt   # save to file
#   changed-files.sh abc123 | scan.sh --target-list /dev/stdin .
#
# Handles:
#   - First commit in repo (no parent): uses git show --name-only
#   - Merge commits: diffs against first parent (--first-parent)
#   - Deleted files: excluded (they don't exist on disk to scan)
#   - Binary files: included (linters can flag them)
#   - Empty result: exits 0 with no output (caller should check)
set -eu
set -o pipefail 2>/dev/null || true

COMMIT_SHA="${1:-}"
OUTPUT_FILE="${2:-}"

if [ -z "$COMMIT_SHA" ]; then
    echo "Usage: changed-files.sh <commit_sha> [output_file]" >&2
    exit 1
fi

# Verify we are in a git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: not inside a git repository" >&2
    exit 1
fi

# Verify the commit exists
if ! git cat-file -t "$COMMIT_SHA" >/dev/null 2>&1; then
    echo "ERROR: commit not found: $COMMIT_SHA" >&2
    exit 1
fi

# Generate the list of changed files using git show (works uniformly for
# normal commits, first commits, and merge commits).
# --diff-filter=d excludes deleted files (they don't exist on disk to scan).
CHANGED=$(git show --name-only --format="" --diff-filter=d \
    "$COMMIT_SHA" 2>/dev/null || echo "")

# Filter out empty lines
CHANGED=$(echo "$CHANGED" | sed '/^$/d')

if [ -z "$CHANGED" ]; then
    # No changed files — not an error, just nothing to scan
    if [ -n "$OUTPUT_FILE" ]; then
        : > "$OUTPUT_FILE"
    fi
    exit 0
fi

# Output
if [ -n "$OUTPUT_FILE" ]; then
    echo "$CHANGED" > "$OUTPUT_FILE"
else
    echo "$CHANGED"
fi

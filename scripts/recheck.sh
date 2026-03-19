#!/usr/bin/env bash
# recheck.sh - On-demand review loop trigger
# Same logic as rechecker.sh but without the PostToolUse JSON parsing.
# Called directly: bash recheck.sh [commit_sha]
set -eu
set -o pipefail 2>/dev/null || true

COMMIT_SHA="${1:-}"
PROJECT_DIR="$(pwd)"

# Resolve commit
if [ -z "$COMMIT_SHA" ]; then
    COMMIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
fi

if [ -z "$COMMIT_SHA" ]; then
    echo "ERROR: No commit found. Are you in a git repository?" >&2
    exit 1
fi

if ! git cat-file -t "$COMMIT_SHA" >/dev/null 2>&1; then
    echo "ERROR: Commit not found: $COMMIT_SHA" >&2
    exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
REPORTS_DIR="${PROJECT_DIR}/reports_dev"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# Acquire lock
LOCK_DIR="${PROJECT_DIR}/.rechecker"
LOCK_FILE="${LOCK_DIR}/rechecker.lock"
mkdir -p "$LOCK_DIR"

if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "Another review cycle is already in progress (PID: $LOCK_PID). Skipping."
        exit 0
    fi
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"

cleanup() {
    rm -f "$LOCK_FILE"
}
trap 'cleanup; exit' INT TERM
trap cleanup EXIT

# Run the review loop
mkdir -p "$REPORTS_DIR"

RESULT=$("${PLUGIN_ROOT}/scripts/review-loop.sh" \
    "$PROJECT_DIR" \
    "$COMMIT_SHA" \
    "$CURRENT_BRANCH" \
    "$REPORTS_DIR" \
    "$TIMESTAMP" \
    "$PLUGIN_ROOT" \
    2>/dev/null) || RESULT="Review loop failed or timed out."

echo "$RESULT"

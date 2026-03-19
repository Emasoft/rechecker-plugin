#!/usr/bin/env bash
# rechecker.sh - PostToolUse hook entry point
# Detects git commit commands, acquires lock, invokes review loop
# set -o pipefail may not be supported on macOS Bash 3.2
set -eu
set -o pipefail 2>/dev/null || true

# ── Dependency check ─────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

# ── Read hook input from stdin ──────────────────────────────────
HOOK_INPUT=$(cat)

# ── Parse JSON fields using python3 (reliable, cross-platform) ─
parse_json_field() {
    local json="$1"
    local field_path="$2"
    echo "$json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    keys = '${field_path}'.split('.')
    val = d
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, '')
        else:
            val = ''
            break
    print(val if val else '')
except:
    print('')
" 2>/dev/null || echo ""
}

TOOL_NAME=$(parse_json_field "$HOOK_INPUT" "tool_name")
COMMAND=$(parse_json_field "$HOOK_INPUT" "tool_input.command")
PROJECT_DIR=$(parse_json_field "$HOOK_INPUT" "cwd")
SESSION_ID=$(parse_json_field "$HOOK_INPUT" "session_id")

# Fallback to env var if cwd not in JSON
PROJECT_DIR="${PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$(pwd)}}"

# ── Gate: only process Bash tool calls ──────────────────────────
if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

# ── Gate: check if command contains a real git commit ───────────
# Handles compound commands (&&, ;, |), rejects --amend
is_git_commit() {
    local cmd="$1"
    python3 -c "
import sys, re

cmd = sys.argv[1]

# If --amend appears ANYWHERE in the full command, reject it entirely.
# This prevents false positives like 'git commit -m msg; git commit --amend'
if re.search(r'--amend', cmd):
    sys.exit(1)

# Split on && and ; and | to handle compound commands
parts = re.split(r'&&|;|\|', cmd)

for part in parts:
    part = part.strip()
    # Match: 'git commit' as a distinct command (not inside a string/comment)
    if re.search(r'\bgit\s+commit\b', part):
        sys.exit(0)

# No git commit found
sys.exit(1)
" "$cmd" 2>/dev/null
}

if ! is_git_commit "$COMMAND"; then
    exit 0
fi

# ── Gate: verify we are in a git repository ─────────────────────
if ! (cd "$PROJECT_DIR" && git rev-parse --is-inside-work-tree >/dev/null 2>&1); then
    exit 0
fi

# ── Gate: verify claude CLI is available ────────────────────────
if ! command -v claude >/dev/null 2>&1; then
    cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "[Rechecker] ERROR: 'claude' CLI not found on PATH. Cannot run automated review."
  }
}
EOF
    exit 0
fi

# ── Acquire lock ────────────────────────────────────────────────
LOCK_DIR="${PROJECT_DIR}/.rechecker"
LOCK_FILE="${LOCK_DIR}/rechecker.lock"
mkdir -p "$LOCK_DIR"

# Check if another review is already running
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        # Another rechecker is still running
        cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "[Rechecker] Skipped: another review cycle is already in progress."
  }
}
EOF
        exit 0
    fi
    # Stale lock file from a crashed process
    rm -f "$LOCK_FILE"
fi

# Write our PID
echo $$ > "$LOCK_FILE"

# Ensure lock is released on exit (normal or error)
cleanup() {
    rm -f "$LOCK_FILE"
}
trap 'cleanup; exit' INT TERM
trap cleanup EXIT

# ── Get commit info ─────────────────────────────────────────────
COMMIT_SHA=$(cd "$PROJECT_DIR" && git rev-parse HEAD 2>/dev/null || echo "")
if [ -z "$COMMIT_SHA" ]; then
    exit 0
fi

CURRENT_BRANCH=$(cd "$PROJECT_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

# ── Prepare reports directory ───────────────────────────────────
REPORTS_DIR="${PROJECT_DIR}/reports_dev"
mkdir -p "$REPORTS_DIR"

# ── Resolve plugin root (for accessing agent definition) ────────
# CLAUDE_PLUGIN_ROOT is set by Claude Code for plugin hooks
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# ── Run the review loop ────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

LOOP_RESULT=$("${PLUGIN_ROOT}/scripts/review-loop.sh" \
    "$PROJECT_DIR" \
    "$COMMIT_SHA" \
    "$CURRENT_BRANCH" \
    "$REPORTS_DIR" \
    "$TIMESTAMP" \
    "$PLUGIN_ROOT" \
    2>/dev/null) || LOOP_RESULT="Review loop failed or timed out."

# ── Construct JSON output with additionalContext ────────────────
# Escape the result for safe JSON embedding
ESCAPED_RESULT=$(python3 -c "
import sys, json
text = sys.stdin.read().strip()
# json.dumps adds quotes, strip them for embedding
print(json.dumps(text)[1:-1])
" <<< "$LOOP_RESULT" 2>/dev/null || echo "Review completed.")

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "[Rechecker] ${ESCAPED_RESULT}"
  }
}
EOF

exit 0

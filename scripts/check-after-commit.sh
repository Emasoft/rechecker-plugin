#!/usr/bin/env bash
# check-after-commit.sh — PostToolUse hook script
# Reads hook JSON from stdin, checks if it's a git commit,
# then launches claude --worktree to review the code asynchronously.

set -uo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
AGENT_FILE="$PLUGIN_ROOT/agents/code-reviewer.md"
FUNC_AGENT_FILE="$PLUGIN_ROOT/agents/functionality-reviewer.md"

# Read hook JSON from stdin and extract fields using python3 one-liner
eval "$(python3 -c "
import json, sys, re
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except: sys.exit(0)
tool = d.get('tool_name', '')
cmd = d.get('tool_input', {}).get('command', '')
cwd = d.get('cwd', '')
# Only process Bash tool with git commit (not --amend)
if tool != 'Bash': sys.exit(0)
if '--amend' in cmd: sys.exit(0)
parts = re.split(r'&&|;|\|', cmd)
has_commit = any(re.search(r'\bgit\s+commit\b', p) for p in parts)
if not has_commit: sys.exit(0)
# Extract cd target if present
m = re.match(r'cd\s+[\"\\']?([^\"\\'\s]+)', cmd)
effective_cwd = m.group(1) if m else cwd
print(f'HOOK_CWD=\"{effective_cwd or cwd}\"')
print(f'HOOK_CMD=\"{cmd.replace(chr(34), chr(92)+chr(34))}\"')
")"

# If python3 exited (not a git commit), we exit too
[[ -z "${HOOK_CWD:-}" ]] && exit 0

# Find the git root
GIT_ROOT=$(cd "$HOOK_CWD" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || {
  # Try walking up from cwd
  d="$HOOK_CWD"
  while [[ "$d" != "/" ]]; do
    [[ -d "$d/.git" ]] && { GIT_ROOT="$d"; break; }
    d=$(dirname "$d")
  done
}
[[ -z "${GIT_ROOT:-}" ]] && exit 0

# Check lock (skip if another review is running)
LOCK_DIR="$GIT_ROOT/.rechecker"
LOCK_FILE="$LOCK_DIR/rechecker.lock"
mkdir -p "$LOCK_DIR"
if [[ -f "$LOCK_FILE" ]]; then
  pid=$(cat "$LOCK_FILE" 2>/dev/null)
  if kill -0 "$pid" 2>/dev/null; then
    exit 0  # Another review is running
  fi
  rm -f "$LOCK_FILE"
fi

# Get commit info
COMMIT_SHA=$(cd "$GIT_ROOT" && git rev-parse HEAD 2>/dev/null) || exit 0
COMMIT_MSG=$(cd "$GIT_ROOT" && git log -1 --format=%s 2>/dev/null)

# Launch Phase 1: code review in a worktree (runs async via hooks.json "async": true)
claude --worktree "rechecker-$(date +%Y%m%d%H%M%S)" \
  --agent "$AGENT_FILE" \
  -p "Review the latest commit ($COMMIT_SHA) and check the code for issues. Fix all the issues found. Commit message: $COMMIT_MSG" \
  --dangerously-skip-permissions &

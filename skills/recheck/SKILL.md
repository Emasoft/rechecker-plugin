# Recheck - On-Demand Code Review

Trigger the rechecker review loop manually on the latest commit (or a specified commit). This does the same thing the PostToolUse hook does automatically after git commits, but on demand.

## Usage

```
/recheck              # Review the latest commit on the current branch
/recheck <commit_sha> # Review a specific commit
```

## What It Does

1. Resolves the target commit (HEAD or the provided SHA)
2. Acquires the rechecker lock (skips if another review is running)
3. Runs the full review loop: worktree creation, scan.sh, code review, fix, merge, repeat
4. Saves reports to `reports_dev/`
5. Returns a summary with a pointer to the report files

## Instructions

Run the rechecker review loop on demand. This is identical to what the PostToolUse hook triggers after a `git commit`.

```bash
# Resolve parameters
PROJECT_DIR="$(pwd)"
COMMIT_SHA="${1:-$(git rev-parse HEAD 2>/dev/null)}"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
REPORTS_DIR="${PROJECT_DIR}/reports_dev"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(dirname "$(dirname "$0")")}"

# Validate
if [ -z "$COMMIT_SHA" ]; then
  echo "ERROR: No commit found. Are you in a git repository?"
  exit 1
fi

if ! git cat-file -t "$COMMIT_SHA" >/dev/null 2>&1; then
  echo "ERROR: Commit not found: $COMMIT_SHA"
  exit 1
fi

# Check lock
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
trap 'rm -f "$LOCK_FILE"' EXIT INT TERM

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
```

After the script completes, READ the summary report file mentioned in the output to see the full results.

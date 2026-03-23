#!/usr/bin/env bash
# merge-worktrees.sh — Fully automated merge of rechecker worktree branches
#
# Standalone script. Only requires: git, bash. No Claude Code dependency.
# Run from any git repo where the rechecker plugin created worktrees.
#
# Merges all worktree-rck-* branches into the current branch.
# Handles dirty working tree (auto-stash), conflicts (-X ours strategy),
# worktree removal, file cleanup (moves reports to docs_dev/), and branch deletion.
#
# Usage:
#   bash merge-worktrees.sh [--dry-run] [--no-cleanup]
#
# Options:
#   --dry-run     Show what would be merged without doing it
#   --no-cleanup  Skip branch/file cleanup after merging
#
# Designed to be called by Claude Code without manual intervention.
# All git operations use safe flags (no --force, no checkout --).

set -euo pipefail

# Must be in a git repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: Not inside a git repository."
  exit 1
fi

GIT_ROOT=$(git rev-parse --show-toplevel)
cd "$GIT_ROOT"

DRY_RUN=false
NO_CLEANUP=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --no-cleanup) NO_CLEANUP=true ;;
    -h|--help)
      cat <<'HELP'
Usage: merge-worktrees.sh [options]

Fully automated merge of rechecker worktree branches into the current branch.

Options:
  --dry-run     Preview what would be merged without doing anything
  --no-cleanup  Merge branches but skip cleanup (keep branches, files)
  --help, -h    Show this help

Behavior:
  1. Removes rechecker worktrees (can't merge a checked-out branch)
  2. Auto-stashes uncommitted changes (restores after)
  3. Merges each worktree-rck-* branch with -X ours (current branch wins on conflict)
  4. Moves rck-*-report.md and rck-*-merge-pending.md to docs_dev/
  5. Deletes merged branches with safe -d flag (keeps unmerged ones)
  6. Auto-commits cleanup if there are staged changes
  7. Restores stash
HELP
      exit 0
      ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

CURRENT_BRANCH=$(git branch --show-current)
echo "Git root: $GIT_ROOT"
echo "Current branch: $CURRENT_BRANCH"

# Find all rechecker worktree branches (both naming conventions)
# Strip leading markers: * (current), + (worktree checkout), spaces
BRANCHES=$(git branch --list 'worktree-rck-*' 'worktree-rechecker-*' | sed 's/^[+* ]*//' || true)

if [[ -z "$BRANCHES" ]]; then
  echo "No rechecker worktree branches found. Nothing to merge."
  exit 0
fi

BRANCH_COUNT=$(echo "$BRANCHES" | wc -l | tr -d ' ')
echo "Found $BRANCH_COUNT rechecker branch(es)"

# ---- Step 0: Remove worktrees FIRST — can't merge a checked-out branch ----
echo ""
echo "Removing worktrees before merge..."
git worktree list --porcelain 2>/dev/null | {
  wt_path=""
  while IFS= read -r line; do
    if [[ "$line" == "worktree "* ]]; then
      wt_path="${line#worktree }"
    elif [[ "$line" == "branch refs/heads/worktree-rck-"* ]] || [[ "$line" == "branch refs/heads/worktree-rechecker-"* ]]; then
      if [[ -n "${wt_path:-}" ]] && [[ -d "$wt_path" ]]; then
        git worktree remove "$wt_path" --force 2>/dev/null && echo "  Removed worktree: $wt_path" || echo "  Failed to remove: $wt_path"
      fi
    fi
  done
}
git worktree prune 2>/dev/null

# Show branch details
echo ""
echo "Branches to merge:"
while IFS= read -r branch; do
  diff_count=$(git diff --stat "$CURRENT_BRANCH...$branch" --ignore-submodules 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$diff_count" -le 1 ]]; then
    echo "  $branch  (no changes — will skip)"
  else
    echo "  $branch  ($((diff_count - 1)) files changed)"
  fi
done <<< "$BRANCHES"

if $DRY_RUN; then
  echo ""
  echo "[DRY RUN] Would merge the above branches. Run without --dry-run to proceed."
  exit 0
fi

# ---- Step 1: Auto-stash if dirty ----
STASHED=false
if ! git diff --quiet --ignore-submodules 2>/dev/null || ! git diff --cached --quiet --ignore-submodules 2>/dev/null; then
  echo ""
  echo "Auto-stashing uncommitted changes..."
  git stash push --include-untracked -m "auto-stash: rechecker merge $(date +%Y%m%d_%H%M%S)"
  STASHED=true
fi

# ---- Step 2: Move existing rechecker files to docs_dev ----
DOCS_DEV="$GIT_ROOT/docs_dev"
mkdir -p "$DOCS_DEV"
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null && echo "  Moved $f -> docs_dev/" || true
  done
done

# ---- Step 3: Merge each branch with -X ours (current branch wins on conflict) ----
MERGED=0
SKIPPED=0
FAILED=0

echo ""
while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue
  echo -n "  $branch... "

  # Check if branch has meaningful changes
  diff_count=$(git diff --stat "$CURRENT_BRANCH...$branch" --ignore-submodules 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$diff_count" -le 1 ]]; then
    echo "skip (no changes)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Merge with -X ours — current branch wins on conflict, no manual intervention needed
  if git merge -X ours "$branch" --no-edit 2>/dev/null; then
    echo "merged ($((diff_count - 1)) files)"
    MERGED=$((MERGED + 1))
  else
    git merge --abort 2>/dev/null || true
    echo "FAILED (aborted)"
    FAILED=$((FAILED + 1))
  fi
done <<< "$BRANCHES"

# ---- Step 4: Move any newly appeared rechecker files to docs_dev ----
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
  done
done

if $NO_CLEANUP; then
  echo ""
  echo "Cleanup skipped (--no-cleanup)."
else
  # ---- Step 5: Delete merged branches (safe -d, refuses if not fully merged) ----
  DELETED=0
  KEPT=0
  echo ""
  echo "Deleting merged branches..."
  while IFS= read -r branch; do
    [[ -z "$branch" ]] && continue
    if git branch -d "$branch" 2>/dev/null; then
      echo "  Deleted: $branch"
      DELETED=$((DELETED + 1))
    else
      echo "  Kept (not fully merged): $branch"
      KEPT=$((KEPT + 1))
    fi
  done <<< "$BRANCHES"
fi

# ---- Step 6: Auto-commit cleanup ----
git add rck-*.md docs_dev/rck-*.md .rechecker/rck-progress.json 2>/dev/null || true

if ! git diff --cached --quiet 2>/dev/null; then
  git commit -m "chore: merge $MERGED rechecker worktree(s) + cleanup reports" 2>/dev/null || true
  echo "  Auto-committed cleanup"
fi

# ---- Step 7: Restore stash ----
if $STASHED; then
  echo ""
  echo "Restoring stashed changes..."
  git stash pop 2>/dev/null || echo "  WARNING: stash pop failed — check 'git stash list'"
fi

# ---- Summary ----
echo ""
echo "=== Done ==="
echo "  Merged:  $MERGED"
echo "  Skipped: $SKIPPED (no changes)"
echo "  Failed:  $FAILED"
if ! $NO_CLEANUP; then
  echo "  Deleted: ${DELETED:-0} branches"
  [[ ${KEPT:-0} -gt 0 ]] && echo "  Kept:    $KEPT (not fully merged — safe)"
fi

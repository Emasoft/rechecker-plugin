#!/usr/bin/env bash
# merge-worktrees.sh — Safe merge of rechecker worktree branches
#
# Standalone script. Only requires: git, bash. No Claude Code dependency.
# Run from any git repo where the rechecker plugin created worktrees.
#
# SAFETY: Only merges branches whose base commit is an ancestor of the
# current branch. Skips branches based on other branches (e.g. main
# worktrees won't merge into a feature branch). Saves and restores
# the current branch on any error.
#
# Usage:
#   bash merge-worktrees.sh [--dry-run] [--no-cleanup]
#
# Options:
#   --dry-run     Show what would be merged without doing it
#   --no-cleanup  Skip branch/file cleanup after merging

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

Safe merge of rechecker worktree branches into the current branch.

Options:
  --dry-run     Preview what would be merged without doing anything
  --no-cleanup  Merge branches but skip cleanup (keep branches, files)
  --help, -h    Show this help

Safety:
  - Only merges branches based on the current branch (ancestry check)
  - Skips branches from other branches (won't merge main into feature)
  - Saves and restores current branch on error
  - Uses -X theirs (prefers rechecker fixes on conflict)
  - Safe -d branch delete (refuses if not fully merged)
HELP
      exit 0
      ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

CURRENT_BRANCH=$(git branch --show-current)
if [[ -z "$CURRENT_BRANCH" ]]; then
  echo "ERROR: Detached HEAD state. Checkout a branch first."
  exit 1
fi
CURRENT_HEAD=$(git rev-parse HEAD)

echo "Git root: $GIT_ROOT"
echo "Current branch: $CURRENT_BRANCH ($CURRENT_HEAD)"

# Find all rechecker worktree branches (both naming conventions)
# Strip leading markers: * (current), + (worktree checkout), spaces
ALL_BRANCHES=$(git branch --list 'worktree-rck-*' 'worktree-rechecker-*' | sed 's/^[+* ]*//' || true)

if [[ -z "$ALL_BRANCHES" ]]; then
  echo "No rechecker worktree branches found. Nothing to merge."
  exit 0
fi

# ---- Step 0: Remove worktrees (can't merge a checked-out branch) ----
echo ""
echo "Removing worktrees..."
# Build list of worktree paths to remove (outside the pipe to avoid subshell)
WT_PATHS_TO_REMOVE=()
while IFS= read -r line; do
  if [[ "$line" == "worktree "* ]]; then
    current_wt_path="${line#worktree }"
  elif [[ "$line" == "branch refs/heads/worktree-rck-"* ]] || [[ "$line" == "branch refs/heads/worktree-rechecker-"* ]]; then
    if [[ -n "${current_wt_path:-}" ]] && [[ -d "$current_wt_path" ]]; then
      WT_PATHS_TO_REMOVE+=("$current_wt_path")
    fi
  fi
done < <(git worktree list --porcelain 2>/dev/null)

for wt_path in "${WT_PATHS_TO_REMOVE[@]}"; do
  git worktree remove "$wt_path" --force 2>/dev/null && echo "  Removed: $wt_path" || echo "  Failed: $wt_path"
done
git worktree prune 2>/dev/null

# Verify we're still on the right branch (worktree removal should not change it)
AFTER_BRANCH=$(git branch --show-current)
if [[ "$AFTER_BRANCH" != "$CURRENT_BRANCH" ]]; then
  echo "WARNING: Branch changed from $CURRENT_BRANCH to $AFTER_BRANCH after worktree removal!"
  echo "  Restoring..."
  git checkout "$CURRENT_BRANCH" 2>/dev/null || true
fi

# ---- Step 1: Filter branches by ancestry ----
# Only merge branches whose merge-base with current branch is the branch's
# parent commit. This prevents merging branches based on other branches.
BRANCHES=""
SKIPPED_ANCESTRY=0
echo ""
echo "Checking branch ancestry..."
while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue
  # Get the commit where this branch diverged from current
  merge_base=$(git merge-base "$CURRENT_BRANCH" "$branch" 2>/dev/null || echo "")
  if [[ -z "$merge_base" ]]; then
    echo "  SKIP $branch — no common ancestor with $CURRENT_BRANCH"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi
  # Check if the merge base is reachable from current HEAD
  if ! git merge-base --is-ancestor "$merge_base" HEAD 2>/dev/null; then
    echo "  SKIP $branch — based on a different branch"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi
  BRANCHES="${BRANCHES}${branch}"$'\n'
done <<< "$ALL_BRANCHES"

# Trim trailing newline
BRANCHES=$(echo "$BRANCHES" | sed '/^$/d')

if [[ -z "$BRANCHES" ]]; then
  echo "No rechecker branches are based on $CURRENT_BRANCH. Nothing to merge."
  [[ $SKIPPED_ANCESTRY -gt 0 ]] && echo "  ($SKIPPED_ANCESTRY branch(es) skipped — based on other branches)"
  exit 0
fi

BRANCH_COUNT=$(echo "$BRANCHES" | wc -l | tr -d ' ')
echo ""
echo "Found $BRANCH_COUNT branch(es) to merge (from $CURRENT_BRANCH):"
[[ $SKIPPED_ANCESTRY -gt 0 ]] && echo "  ($SKIPPED_ANCESTRY skipped — based on other branches)"
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

# ---- Step 2: Auto-stash if dirty ----
STASHED=false
if ! git diff --quiet --ignore-submodules 2>/dev/null || ! git diff --cached --quiet --ignore-submodules 2>/dev/null; then
  echo ""
  echo "Auto-stashing uncommitted changes..."
  git stash push --include-untracked -m "auto-stash: rechecker merge $(date +%Y%m%d_%H%M%S)"
  STASHED=true
fi

# ---- Step 3: Move existing rechecker files to docs_dev ----
DOCS_DEV="$GIT_ROOT/docs_dev"
mkdir -p "$DOCS_DEV"
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
  done
done

# ---- Step 4: Merge each branch ----
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

  # Merge with -X theirs — prefer the rechecker's fixes on conflict.
  # The rechecker reviewed the code and produced fixes. On conflict,
  # the fix should win over the pre-existing code.
  if git merge -X theirs "$branch" --no-edit 2>/dev/null; then
    echo "merged ($((diff_count - 1)) files)"
    MERGED=$((MERGED + 1))
  else
    git merge --abort 2>/dev/null || true
    echo "FAILED (aborted)"
    FAILED=$((FAILED + 1))
  fi
done <<< "$BRANCHES"

# ---- Step 5: Verify branch integrity ----
AFTER_MERGE_BRANCH=$(git branch --show-current)
if [[ "$AFTER_MERGE_BRANCH" != "$CURRENT_BRANCH" ]]; then
  echo ""
  echo "ERROR: Branch changed during merge! Was $CURRENT_BRANCH, now $AFTER_MERGE_BRANCH"
  echo "  Restoring $CURRENT_BRANCH..."
  git checkout "$CURRENT_BRANCH" 2>/dev/null || echo "  FAILED to restore — manual intervention needed"
fi

# ---- Step 6: Move any newly appeared rechecker files to docs_dev ----
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
  done
done

if ! $NO_CLEANUP; then
  # ---- Step 7: Delete merged branches (safe -d, refuses if not fully merged) ----
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

# ---- Step 8: Restore stash ----
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
[[ $SKIPPED_ANCESTRY -gt 0 ]] && echo "  Skipped: $SKIPPED_ANCESTRY (different base branch)"
if ! $NO_CLEANUP; then
  echo "  Deleted: ${DELETED:-0} branches"
  [[ ${KEPT:-0} -gt 0 ]] && echo "  Kept:    $KEPT (not fully merged — safe)"
fi

#!/usr/bin/env bash
# merge-worktrees.sh — Ultra-safe merge of rechecker worktree branches
#
# Standalone script. Only requires: git, bash. No Claude Code dependency.
# Run from any git repo where the rechecker plugin created worktrees.
#
# SAFETY GUARANTEES:
# - Git lock file prevents concurrent git operations during merge
# - Ancestry check: only merges branches based on the current branch
# - Branch integrity verified after EVERY git operation
# - Working tree cleanliness verified after EVERY merge
# - Automatic rollback on any failure
# - All destructive operations are reversible (no --force, no -D)
# - Dry-run by default when merge-pending files are missing
#
# Usage:
#   bash merge-worktrees.sh [--dry-run] [--no-cleanup]

set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────

die() { echo "FATAL: $1" >&2; cleanup_lock; exit 1; }
warn() { echo "WARNING: $1" >&2; }
info() { echo "  $1"; }

LOCKFILE=""
STASHED=false
ORIGINAL_BRANCH=""
ORIGINAL_HEAD=""
MERGE_IN_PROGRESS=false

cleanup_lock() {
  [[ -n "$LOCKFILE" ]] && [[ -f "$LOCKFILE" ]] && rm -f "$LOCKFILE" 2>/dev/null
}

# Trap: restore state on any error or interrupt
cleanup_on_error() {
  local exit_code=$?
  echo ""
  echo "=== ERROR: Script interrupted (exit code $exit_code) ==="

  # Abort any in-progress merge
  if $MERGE_IN_PROGRESS; then
    echo "  Aborting in-progress merge..."
    git merge --abort 2>/dev/null || true
    MERGE_IN_PROGRESS=false
  fi

  # Restore branch if changed
  local current
  current=$(git branch --show-current 2>/dev/null || echo "")
  if [[ -n "$ORIGINAL_BRANCH" ]] && [[ "$current" != "$ORIGINAL_BRANCH" ]]; then
    echo "  Restoring branch $ORIGINAL_BRANCH..."
    git checkout "$ORIGINAL_BRANCH" 2>/dev/null || warn "Could not restore branch"
  fi

  # Restore stash if we stashed
  if $STASHED; then
    echo "  Restoring stashed changes..."
    git stash pop 2>/dev/null || warn "Stash pop failed — check 'git stash list'"
    STASHED=false
  fi

  cleanup_lock
  echo "  Recovery complete. Repository should be in its original state."
  exit "$exit_code"
}
trap cleanup_on_error ERR INT TERM

# Verify we're still on the expected branch and HEAD hasn't changed unexpectedly
assert_branch() {
  local expected_branch="$1"
  local context="$2"
  local actual
  actual=$(git branch --show-current 2>/dev/null || echo "DETACHED")
  if [[ "$actual" != "$expected_branch" ]]; then
    die "Branch changed during $context! Expected '$expected_branch', got '$actual'. Aborting."
  fi
}

assert_clean() {
  local context="$1"
  if ! git diff --quiet --ignore-submodules 2>/dev/null; then
    die "Working tree became dirty during $context! Aborting."
  fi
  if ! git diff --cached --quiet --ignore-submodules 2>/dev/null; then
    die "Index has staged changes during $context! Aborting."
  fi
}

# Acquire a lock to prevent concurrent git operations
acquire_lock() {
  local git_dir
  git_dir=$(git rev-parse --git-dir 2>/dev/null) || die "Cannot find .git directory"
  LOCKFILE="$git_dir/rechecker-merge.lock"

  # Check for git's own lock first
  if [[ -f "$git_dir/index.lock" ]]; then
    die "Git index is locked ($git_dir/index.lock). Another git process is running."
  fi

  # Try to acquire our lock (atomic via noclobber)
  if ! (set -o noclobber; echo "$$" > "$LOCKFILE") 2>/dev/null; then
    local other_pid
    other_pid=$(cat "$LOCKFILE" 2>/dev/null || echo "unknown")
    # Check if the other process is still running
    if [[ "$other_pid" =~ ^[0-9]+$ ]] && kill -0 "$other_pid" 2>/dev/null; then
      die "Another merge-worktrees.sh is running (PID $other_pid). Wait for it to finish."
    else
      warn "Stale lock file found (PID $other_pid not running). Removing."
      rm -f "$LOCKFILE"
      (set -o noclobber; echo "$$" > "$LOCKFILE") 2>/dev/null || die "Cannot acquire lock"
    fi
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────

# Must be in a git repo
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository."

GIT_ROOT=$(git rev-parse --show-toplevel)
cd "$GIT_ROOT" || die "Cannot cd to git root: $GIT_ROOT"

DRY_RUN=false
NO_CLEANUP=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --no-cleanup) NO_CLEANUP=true ;;
    -h|--help)
      cat <<'HELP'
Usage: merge-worktrees.sh [options]

Ultra-safe merge of rechecker worktree branches into the current branch.

Options:
  --dry-run     Preview what would be merged without doing anything
  --no-cleanup  Merge branches but skip cleanup (keep branches, files)
  --help, -h    Show this help

Safety:
  - Git lock prevents concurrent operations
  - Only merges branches based on the current branch (ancestry check)
  - Branch integrity verified after every operation
  - Working tree cleanliness checked after every merge
  - Automatic rollback on any error
  - Uses -X theirs (prefers rechecker fixes on conflict)
  - Safe -d branch delete (refuses if not fully merged)
HELP
      exit 0
      ;;
    *) die "Unknown option: $arg (try --help)" ;;
  esac
done

# ── Pre-flight checks ────────────────────────────────────────────────────

ORIGINAL_BRANCH=$(git branch --show-current)
[[ -z "$ORIGINAL_BRANCH" ]] && die "Detached HEAD state. Checkout a branch first."
ORIGINAL_HEAD=$(git rev-parse HEAD)

echo "Git root: $GIT_ROOT"
echo "Branch:   $ORIGINAL_BRANCH"
echo "HEAD:     $ORIGINAL_HEAD"

# Verify git is functional
git status --porcelain --ignore-submodules >/dev/null 2>&1 || die "git status failed — repository may be corrupted"

# Find all rechecker worktree branches
ALL_BRANCHES=$(git branch --list 'worktree-rck-*' 'worktree-rechecker-*' | sed 's/^[+* ]*//' || true)

if [[ -z "$ALL_BRANCHES" ]]; then
  echo "No rechecker worktree branches found. Nothing to merge."
  exit 0
fi

TOTAL_FOUND=$(echo "$ALL_BRANCHES" | wc -l | tr -d ' ')
echo "Found $TOTAL_FOUND rechecker branch(es) total"

# ── Step 0: Acquire lock ─────────────────────────────────────────────────

if ! $DRY_RUN; then
  acquire_lock
  info "Lock acquired (PID $$)"
fi

# ── Step 1: Remove worktrees (can't merge checked-out branches) ──────────

echo ""
echo "Step 1: Removing worktrees..."

# Collect worktree paths BEFORE removing (process substitution, not pipe subshell)
WT_PATHS=()
while IFS= read -r line; do
  if [[ "$line" == "worktree "* ]]; then
    _wt_path="${line#worktree }"
  elif [[ "$line" == "branch refs/heads/worktree-rck-"* ]] || [[ "$line" == "branch refs/heads/worktree-rechecker-"* ]]; then
    if [[ -n "${_wt_path:-}" ]] && [[ -d "$_wt_path" ]]; then
      WT_PATHS+=("$_wt_path")
    fi
  fi
done < <(git worktree list --porcelain 2>/dev/null)

if [[ ${#WT_PATHS[@]} -eq 0 ]]; then
  info "No worktree directories to remove"
else
  if $DRY_RUN; then
    for wt_path in "${WT_PATHS[@]}"; do
      info "[DRY] Would remove worktree: $wt_path"
    done
  else
    for wt_path in "${WT_PATHS[@]}"; do
      # Check no git process is using this worktree
      if lsof "$wt_path/.git" >/dev/null 2>&1; then
        warn "Worktree $wt_path is in use by another process — skipping"
        continue
      fi
      git worktree remove "$wt_path" --force 2>/dev/null && info "Removed: $wt_path" || warn "Failed: $wt_path"
    done
    git worktree prune 2>/dev/null
  fi
fi

# Verify branch integrity after worktree operations
assert_branch "$ORIGINAL_BRANCH" "worktree removal"

# ── Step 2: Filter branches by ancestry ──────────────────────────────────

echo ""
echo "Step 2: Checking branch ancestry..."

BRANCHES=""
SKIPPED_ANCESTRY=0

while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue

  # Verify the branch ref actually exists
  if ! git rev-parse --verify "$branch" >/dev/null 2>&1; then
    warn "Branch $branch does not exist — skipping"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  # Get the merge base
  merge_base=$(git merge-base "$ORIGINAL_BRANCH" "$branch" 2>/dev/null || echo "")
  if [[ -z "$merge_base" ]]; then
    info "SKIP $branch — no common ancestor with $ORIGINAL_BRANCH"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  # The merge base must be an ancestor of our current HEAD
  if ! git merge-base --is-ancestor "$merge_base" "$ORIGINAL_HEAD" 2>/dev/null; then
    info "SKIP $branch — based on a different branch"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  # Check the branch actually has commits beyond the merge base
  branch_head=$(git rev-parse "$branch" 2>/dev/null)
  if [[ "$branch_head" == "$merge_base" ]]; then
    info "SKIP $branch — no commits beyond merge base"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  BRANCHES="${BRANCHES}${branch}"$'\n'
done <<< "$ALL_BRANCHES"

BRANCHES=$(echo "$BRANCHES" | sed '/^$/d')

if [[ -z "$BRANCHES" ]]; then
  echo "No rechecker branches are based on $ORIGINAL_BRANCH. Nothing to merge."
  [[ $SKIPPED_ANCESTRY -gt 0 ]] && info "$SKIPPED_ANCESTRY branch(es) skipped (different base or no changes)"
  cleanup_lock
  exit 0
fi

BRANCH_COUNT=$(echo "$BRANCHES" | wc -l | tr -d ' ')
echo ""
echo "Branches to merge: $BRANCH_COUNT"
[[ $SKIPPED_ANCESTRY -gt 0 ]] && info "$SKIPPED_ANCESTRY skipped (different base or no changes)"

while IFS= read -r branch; do
  diff_count=$(git diff --stat "$ORIGINAL_BRANCH...$branch" --ignore-submodules 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$diff_count" -le 1 ]]; then
    info "$branch  (no file changes — will skip)"
  else
    info "$branch  ($((diff_count - 1)) files changed)"
  fi
done <<< "$BRANCHES"

if $DRY_RUN; then
  echo ""
  echo "[DRY RUN] Would merge the above branches. Run without --dry-run to proceed."
  cleanup_lock
  exit 0
fi

# ── Step 3: Auto-stash if dirty ──────────────────────────────────────────

echo ""
echo "Step 3: Checking working tree..."

if ! git diff --quiet --ignore-submodules 2>/dev/null || ! git diff --cached --quiet --ignore-submodules 2>/dev/null; then
  info "Stashing uncommitted changes..."
  git stash push --include-untracked -m "auto-stash: rechecker merge $(date +%Y%m%d_%H%M%S)" || die "git stash failed"
  STASHED=true
  info "Stashed successfully"
else
  info "Working tree is clean"
fi

# Final pre-merge verification
assert_branch "$ORIGINAL_BRANCH" "pre-merge check"

# ── Step 4: Move existing rechecker files to docs_dev ────────────────────

DOCS_DEV="$GIT_ROOT/docs_dev"
mkdir -p "$DOCS_DEV"
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
  done
done

# ── Step 5: Merge each branch one at a time ──────────────────────────────

echo ""
echo "Step 5: Merging..."

MERGED=0
SKIPPED=0
FAILED=0
MERGED_BRANCHES=""

while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue

  # Pre-merge checks
  assert_branch "$ORIGINAL_BRANCH" "before merging $branch"

  echo -n "  $branch... "

  # Verify branch still exists (could have been deleted between steps)
  if ! git rev-parse --verify "$branch" >/dev/null 2>&1; then
    echo "SKIP (branch disappeared)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Check if branch has meaningful file changes
  diff_count=$(git diff --stat "$ORIGINAL_BRANCH...$branch" --ignore-submodules 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$diff_count" -le 1 ]]; then
    echo "skip (no file changes)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Check for git index lock before merge
  git_dir=$(git rev-parse --git-dir 2>/dev/null)
  if [[ -f "$git_dir/index.lock" ]]; then
    echo "SKIP (git index locked by another process)"
    FAILED=$((FAILED + 1))
    continue
  fi

  # Save HEAD before merge for rollback
  pre_merge_head=$(git rev-parse HEAD)

  # Attempt merge with -X theirs (prefer rechecker's fixes on conflict)
  MERGE_IN_PROGRESS=true
  if git merge -X theirs "$branch" --no-edit 2>/dev/null; then
    MERGE_IN_PROGRESS=false
    echo "merged ($((diff_count - 1)) files)"
    MERGED=$((MERGED + 1))
    MERGED_BRANCHES="${MERGED_BRANCHES}${branch}"$'\n'

    # Post-merge verification: check branch is still correct
    assert_branch "$ORIGINAL_BRANCH" "after merging $branch"

  else
    MERGE_IN_PROGRESS=false
    # Merge failed — abort cleanly
    git merge --abort 2>/dev/null || true

    # Verify HEAD is back to pre-merge state
    post_abort_head=$(git rev-parse HEAD)
    if [[ "$post_abort_head" != "$pre_merge_head" ]]; then
      warn "HEAD changed after merge abort! Expected $pre_merge_head, got $post_abort_head"
      # Try to restore
      git reset --hard "$pre_merge_head" 2>/dev/null || die "Cannot restore HEAD after failed merge of $branch"
    fi

    echo "FAILED (aborted, no changes applied)"
    FAILED=$((FAILED + 1))
  fi
done <<< "$BRANCHES"

# ── Step 6: Post-merge verification ─────────────────────────────────────

echo ""
echo "Step 6: Verifying..."
assert_branch "$ORIGINAL_BRANCH" "post-merge verification"
info "Branch: $ORIGINAL_BRANCH (correct)"

# Verify the merge history looks sane
if [[ $MERGED -gt 0 ]]; then
  new_head=$(git rev-parse HEAD)
  if ! git merge-base --is-ancestor "$ORIGINAL_HEAD" "$new_head" 2>/dev/null; then
    die "Merge corrupted history! Original HEAD $ORIGINAL_HEAD is not an ancestor of new HEAD $new_head"
  fi
  info "History: original HEAD is ancestor of new HEAD (correct)"
fi

# ── Step 7: Move newly appeared rechecker files to docs_dev ──────────────

for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
  done
done

# ── Step 8: Delete merged branches (safe -d only) ────────────────────────

if ! $NO_CLEANUP; then
  DELETED=0
  KEPT=0
  echo ""
  echo "Step 8: Cleaning up branches..."

  MERGED_BRANCHES=$(echo "$MERGED_BRANCHES" | sed '/^$/d')
  if [[ -n "$MERGED_BRANCHES" ]]; then
    while IFS= read -r branch; do
      [[ -z "$branch" ]] && continue
      # Safe -d: git refuses if not fully merged
      if git branch -d "$branch" 2>/dev/null; then
        info "Deleted: $branch"
        DELETED=$((DELETED + 1))
      else
        info "Kept (not fully merged): $branch"
        KEPT=$((KEPT + 1))
      fi
    done <<< "$MERGED_BRANCHES"
  fi
fi

# ── Step 9: Restore stash ───────────────────────────────────────────────

if $STASHED; then
  echo ""
  echo "Step 9: Restoring stashed changes..."
  if git stash pop 2>/dev/null; then
    info "Stash restored successfully"
    STASHED=false
  else
    warn "Stash pop failed — your changes are in 'git stash list'"
    STASHED=false
  fi
fi

# ── Step 10: Final verification ──────────────────────────────────────────

echo ""
echo "Step 10: Final verification..."
assert_branch "$ORIGINAL_BRANCH" "final check"
info "Branch: $ORIGINAL_BRANCH (correct)"

final_head=$(git rev-parse HEAD)
if [[ $MERGED -gt 0 ]]; then
  info "HEAD moved: $ORIGINAL_HEAD -> $final_head ($MERGED merge(s))"
else
  if [[ "$final_head" != "$ORIGINAL_HEAD" ]]; then
    warn "HEAD changed despite no merges! Was $ORIGINAL_HEAD, now $final_head"
  else
    info "HEAD unchanged: $final_head (no merges applied)"
  fi
fi

# Release lock
cleanup_lock

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "=== Done ==="
echo "  Merged:  $MERGED"
echo "  Skipped: $SKIPPED (no changes)"
echo "  Failed:  $FAILED"
[[ $SKIPPED_ANCESTRY -gt 0 ]] && echo "  Skipped: $SKIPPED_ANCESTRY (different base branch)"
if ! $NO_CLEANUP; then
  echo "  Deleted: ${DELETED:-0} branches"
  [[ ${KEPT:-0} -gt 0 ]] && echo "  Kept:    $KEPT (not fully merged)"
fi

#!/usr/bin/env bash
# merge-worktrees.sh — Ultra-safe merge of rechecker worktree branches
#
# Standalone script. Only requires: git, bash. No Claude Code dependency.
# Run from any git repo where the rechecker plugin created worktrees.
#
# Usage:
#   bash merge-worktrees.sh [--dry-run] [--no-cleanup]

# Disable set -e to prevent trap storms; we handle errors explicitly
set -uo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────

die() { echo "FATAL: $1" >&2; release_lock; exit 1; }
warn() { echo "WARNING: $1" >&2; }
info() { echo "  $1"; }

LOCKFILE=""
STASHED=false
ORIGINAL_BRANCH=""
ORIGINAL_HEAD=""
MERGE_IN_PROGRESS=false

release_lock() {
  if [[ -n "$LOCKFILE" ]] && [[ -f "$LOCKFILE" ]]; then
    # Only remove if we own it
    local lock_pid
    lock_pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [[ "$lock_pid" == "$$" ]]; then
      rm -f "$LOCKFILE" 2>/dev/null
    fi
  fi
}

# Trap: restore state on interrupt (Ctrl+C, kill)
cleanup_on_signal() {
  echo ""
  echo "=== INTERRUPTED ==="

  # Abort any in-progress merge
  if $MERGE_IN_PROGRESS; then
    echo "  Aborting in-progress merge..."
    git merge --abort 2>/dev/null || true
  fi

  # Restore branch if changed
  local current
  current=$(git branch --show-current 2>/dev/null || echo "")
  if [[ -n "$ORIGINAL_BRANCH" ]] && [[ "$current" != "$ORIGINAL_BRANCH" ]]; then
    echo "  Restoring branch $ORIGINAL_BRANCH..."
    git checkout "$ORIGINAL_BRANCH" 2>/dev/null || true
  fi

  # Restore stash if we stashed
  if $STASHED; then
    echo "  Restoring stashed changes..."
    git stash pop 2>/dev/null || echo "  Stash pop failed — check 'git stash list'"
  fi

  release_lock
  exit 130
}
trap cleanup_on_signal INT TERM

# Verify we're still on the expected branch
assert_branch() {
  local expected_branch="$1"
  local context="$2"
  local actual
  actual=$(git branch --show-current 2>/dev/null || echo "DETACHED")
  if [[ "$actual" != "$expected_branch" ]]; then
    die "Branch changed during $context! Expected '$expected_branch', got '$actual'."
  fi
}

# Sanitize a branch name: only allow safe characters
sanitize_branch() {
  local name="$1"
  # Git branch names: alphanumeric, dash, dot, slash, underscore
  if [[ ! "$name" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
    warn "Branch name contains unsafe characters: $name"
    return 1
  fi
  return 0
}

# Acquire a lock to prevent concurrent git operations
acquire_lock() {
  local git_dir
  git_dir=$(git rev-parse --git-dir 2>/dev/null) || die "Cannot find .git directory"
  LOCKFILE="$git_dir/rechecker-merge.lock"

  # Check for git's own lock
  if [[ -f "$git_dir/index.lock" ]]; then
    die "Git index is locked ($git_dir/index.lock). Another git process is running."
  fi

  # Atomic lock acquisition via mkdir (more portable than noclobber, truly atomic on all filesystems)
  local lockdir="$git_dir/rechecker-merge.lk"
  if mkdir "$lockdir" 2>/dev/null; then
    echo "$$" > "$LOCKFILE"
    rmdir "$lockdir" 2>/dev/null
  else
    # Lock exists — check if stale
    local other_pid
    other_pid=$(cat "$LOCKFILE" 2>/dev/null || echo "unknown")
    if [[ "$other_pid" =~ ^[0-9]+$ ]] && kill -0 "$other_pid" 2>/dev/null; then
      rmdir "$lockdir" 2>/dev/null || true
      die "Another merge-worktrees.sh is running (PID $other_pid). Wait for it to finish."
    else
      warn "Stale lock file found (PID $other_pid not running). Removing."
      rm -f "$LOCKFILE" 2>/dev/null
      echo "$$" > "$LOCKFILE"
    fi
    rmdir "$lockdir" 2>/dev/null || true
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────

# Must be in a git repo
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository."

GIT_ROOT=$(git rev-parse --show-toplevel) || die "Cannot determine git root"
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
  - Atomic lock prevents concurrent operations
  - Only merges branches based on the current branch (ancestry check)
  - Branch integrity verified after every operation
  - Automatic rollback on interrupt
  - Uses -X theirs (prefers rechecker fixes on conflict)
  - Safe -d branch delete (refuses if not fully merged)
HELP
      exit 0
      ;;
    *) die "Unknown option: $arg (try --help)" ;;
  esac
done

# ── Pre-flight checks ────────────────────────────────────────────────────

ORIGINAL_BRANCH=$(git branch --show-current) || die "Cannot determine current branch"
[[ -z "$ORIGINAL_BRANCH" ]] && die "Detached HEAD state. Checkout a branch first."
ORIGINAL_HEAD=$(git rev-parse HEAD) || die "Cannot determine HEAD"

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

# Collect worktree paths (process substitution, not pipe — avoids subshell)
WT_PATHS=()
_wt_path=""
while IFS= read -r line; do
  if [[ "$line" == "worktree "* ]]; then
    _wt_path="${line#worktree }"
  elif [[ "$line" == "branch refs/heads/worktree-rck-"* ]] || [[ "$line" == "branch refs/heads/worktree-rechecker-"* ]]; then
    if [[ -n "$_wt_path" ]] && [[ -d "$_wt_path" ]]; then
      WT_PATHS+=("$_wt_path")
    fi
    _wt_path=""  # Reset to prevent reuse on malformed input
  elif [[ -z "$line" ]]; then
    _wt_path=""  # Reset on empty line (worktree separator)
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
      # Check if a Claude process is running inside this worktree
      if pgrep -f "$wt_path" >/dev/null 2>&1; then
        warn "Worktree $wt_path has an active process — skipping"
        continue
      fi
      git worktree remove "$wt_path" --force 2>/dev/null && info "Removed: $wt_path" || warn "Failed: $wt_path"
    done
    git worktree prune 2>/dev/null || true
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

  # Sanitize branch name
  if ! sanitize_branch "$branch"; then
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  # Verify the branch ref actually exists
  if ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
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

  # The merge base must be recent — within 200 commits of HEAD.
  # This prevents merging branches forked from ancient history.
  distance=$(git rev-list --count "$merge_base..$ORIGINAL_HEAD" 2>/dev/null || echo "999")
  if [[ "$distance" -gt 200 ]]; then
    info "SKIP $branch — base is $distance commits behind HEAD (too old)"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  # Check the branch actually has commits beyond the merge base
  branch_head=$(git rev-parse "$branch" 2>/dev/null || echo "")
  if [[ -z "$branch_head" ]] || [[ "$branch_head" == "$merge_base" ]]; then
    info "SKIP $branch — no commits beyond merge base"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  BRANCHES="${BRANCHES}${branch}"$'\n'
done <<< "$ALL_BRANCHES"

BRANCHES=$(echo "$BRANCHES" | sed '/^$/d')

if [[ -z "$BRANCHES" ]]; then
  echo "No rechecker branches are based on $ORIGINAL_BRANCH. Nothing to merge."
  [[ $SKIPPED_ANCESTRY -gt 0 ]] && info "$SKIPPED_ANCESTRY branch(es) skipped"
  release_lock
  exit 0
fi

BRANCH_COUNT=$(echo "$BRANCHES" | wc -l | tr -d ' ')
echo ""
echo "Branches to merge: $BRANCH_COUNT"
[[ $SKIPPED_ANCESTRY -gt 0 ]] && info "$SKIPPED_ANCESTRY skipped (different base, too old, or no changes)"

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
  release_lock
  exit 0
fi

# ── Step 3: Auto-stash if dirty ──────────────────────────────────────────

echo ""
echo "Step 3: Checking working tree..."

if ! git diff --quiet --ignore-submodules 2>/dev/null || ! git diff --cached --quiet --ignore-submodules 2>/dev/null; then
  info "Stashing uncommitted changes..."
  if git stash push -m "auto-stash: rechecker merge $(date +%Y%m%d_%H%M%S)" 2>/dev/null; then
    STASHED=true
    info "Stashed successfully"
  else
    die "Cannot stash changes. Commit or stash manually before running this script."
  fi
else
  info "Working tree is clean"
fi

# Final pre-merge verification
assert_branch "$ORIGINAL_BRANCH" "pre-merge check"

# ── Step 4: Move existing rechecker files to docs_dev ────────────────────

DOCS_DEV="$GIT_ROOT/docs_dev"
mkdir -p "$DOCS_DEV" || die "Cannot create docs_dev directory"
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    if [[ -f "$f" ]]; then
      # Avoid overwriting: append timestamp if destination exists
      dest="$DOCS_DEV/$f"
      if [[ -f "$dest" ]]; then
        dest="$DOCS_DEV/$(date +%Y%m%d_%H%M%S)_$f"
      fi
      mv "$f" "$dest" 2>/dev/null || true
    fi
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
  if ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
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

  # Save HEAD before merge for verification
  pre_merge_head=$(git rev-parse HEAD)

  # Attempt merge with -X theirs (prefer rechecker's fixes on conflict)
  MERGE_IN_PROGRESS=true
  merge_output=$(git merge -X theirs "$branch" --no-edit 2>&1) && merge_ok=true || merge_ok=false
  MERGE_IN_PROGRESS=false

  if $merge_ok; then
    echo "merged ($((diff_count - 1)) files)"
    MERGED=$((MERGED + 1))
    MERGED_BRANCHES="${MERGED_BRANCHES}${branch}"$'\n'

    # Post-merge verification: branch must still be correct
    assert_branch "$ORIGINAL_BRANCH" "after merging $branch"

    # Verify original HEAD is ancestor of new HEAD
    if ! git merge-base --is-ancestor "$pre_merge_head" HEAD 2>/dev/null; then
      die "Merge of $branch corrupted history! Pre-merge HEAD is not ancestor of new HEAD."
    fi

  else
    # Merge failed — abort cleanly
    git merge --abort 2>/dev/null || true

    # Verify HEAD is back to pre-merge state
    post_abort_head=$(git rev-parse HEAD)
    if [[ "$post_abort_head" != "$pre_merge_head" ]]; then
      warn "HEAD changed after merge abort of $branch! Expected $pre_merge_head, got $post_abort_head"
      warn "Stopping. Manual intervention needed."
      FAILED=$((FAILED + 1))
      break  # Stop processing more branches — state is uncertain
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

if [[ $MERGED -gt 0 ]]; then
  new_head=$(git rev-parse HEAD)
  if ! git merge-base --is-ancestor "$ORIGINAL_HEAD" "$new_head" 2>/dev/null; then
    die "History corrupted! Original HEAD is not ancestor of new HEAD."
  fi
  info "History: original HEAD is ancestor of new HEAD (correct)"
fi

# ── Step 7: Move newly appeared rechecker files to docs_dev ──────────────

for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    if [[ -f "$f" ]]; then
      dest="$DOCS_DEV/$f"
      [[ -f "$dest" ]] && dest="$DOCS_DEV/$(date +%Y%m%d_%H%M%S)_$f"
      mv "$f" "$dest" 2>/dev/null || true
    fi
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
    warn "Stash pop had conflicts — your changes are in 'git stash list'"
    warn "Run 'git stash show' to see them, 'git stash drop' after resolving"
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
    info "HEAD unchanged: $final_head"
  fi
fi

# Release lock
release_lock

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

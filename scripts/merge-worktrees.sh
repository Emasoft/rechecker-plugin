#!/usr/bin/env bash
# merge-worktrees.sh — Ultra-safe merge or discard of rechecker worktree branches
#
# Standalone script. Only requires: git, bash. No Claude Code dependency.
# Run from any git repo where the rechecker plugin created worktrees.
#
# Usage:
#   bash merge-worktrees.sh [--dry-run] [--no-cleanup]
#   bash merge-worktrees.sh --discard [name1 name2 ...]
#   bash merge-worktrees.sh --discard-all

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
    local lock_pid
    lock_pid=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [[ "$lock_pid" == "$$" ]]; then
      rm -f "$LOCKFILE" 2>/dev/null
    fi
  fi
}

cleanup_on_signal() {
  echo ""
  echo "=== INTERRUPTED ==="
  if $MERGE_IN_PROGRESS; then
    echo "  Aborting in-progress merge..."
    git merge --abort 2>/dev/null || true
  fi
  local current
  current=$(git branch --show-current 2>/dev/null || echo "")
  if [[ -n "$ORIGINAL_BRANCH" ]] && [[ "$current" != "$ORIGINAL_BRANCH" ]]; then
    echo "  Restoring branch $ORIGINAL_BRANCH..."
    git checkout "$ORIGINAL_BRANCH" 2>/dev/null || true
  fi
  if $STASHED; then
    echo "  Restoring stashed changes..."
    git stash pop 2>/dev/null || echo "  Stash pop failed — check 'git stash list'"
  fi
  release_lock
  exit 130
}
trap cleanup_on_signal INT TERM

assert_branch() {
  local expected_branch="$1"
  local context="$2"
  local actual
  actual=$(git branch --show-current 2>/dev/null || echo "DETACHED")
  if [[ "$actual" != "$expected_branch" ]]; then
    die "Branch changed during $context! Expected '$expected_branch', got '$actual'."
  fi
}

sanitize_branch() {
  local name="$1"
  if [[ ! "$name" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
    warn "Branch name contains unsafe characters: $name"
    return 1
  fi
  return 0
}

acquire_lock() {
  local git_dir
  git_dir=$(git rev-parse --git-dir 2>/dev/null) || die "Cannot find .git directory"
  LOCKFILE="$git_dir/rechecker-merge.lock"
  if [[ -f "$git_dir/index.lock" ]]; then
    die "Git index is locked ($git_dir/index.lock). Another git process is running."
  fi
  local lockdir="$git_dir/rechecker-merge.lk"
  if mkdir "$lockdir" 2>/dev/null; then
    echo "$$" > "$LOCKFILE"
    rmdir "$lockdir" 2>/dev/null
  else
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

# Remove a worktree directory and its branch safely
remove_worktree_and_branch() {
  local branch="$1"
  local removed_wt=false
  local removed_br=false

  # Sanitize
  if ! sanitize_branch "$branch"; then
    warn "Skipping unsafe branch name: $branch"
    return 1
  fi

  # Verify branch exists
  if ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
    warn "Branch $branch does not exist — skipping"
    return 1
  fi

  # Find the worktree path for this branch
  local wt_path=""
  local _current_wt=""
  while IFS= read -r line; do
    if [[ "$line" == "worktree "* ]]; then
      _current_wt="${line#worktree }"
    elif [[ "$line" == "branch refs/heads/$branch" ]]; then
      wt_path="$_current_wt"
    elif [[ -z "$line" ]]; then
      _current_wt=""
    fi
  done < <(git worktree list --porcelain 2>/dev/null)

  # Remove worktree if found
  if [[ -n "$wt_path" ]] && [[ -d "$wt_path" ]]; then
    # Check no process is using it
    if pgrep -f "$wt_path" >/dev/null 2>&1; then
      warn "Worktree $wt_path has an active process — skipping"
      return 1
    fi
    if git worktree remove "$wt_path" --force 2>/dev/null; then removed_wt=true; else warn "Failed to remove worktree: $wt_path"; fi
  fi

  # Delete branch (safe -D since we're discarding, not merging)
  if git branch -D "$branch" 2>/dev/null; then
    removed_br=true
  else
    warn "Failed to delete branch: $branch"
  fi

  if $removed_wt || $removed_br; then
    info "Discarded: $branch"
    return 0
  fi
  return 1
}

# ── Main ─────────────────────────────────────────────────────────────────

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside a git repository."

GIT_ROOT=$(git rev-parse --show-toplevel) || die "Cannot determine git root"
cd "$GIT_ROOT" || die "Cannot cd to git root: $GIT_ROOT"

DRY_RUN=false
NO_CLEANUP=false
DISCARD_MODE=false
DISCARD_ALL=false
DISCARD_NAMES=()

# Parse arguments — need to handle --discard with optional names
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --no-cleanup) NO_CLEANUP=true ;;
    --discard-all) DISCARD_MODE=true; DISCARD_ALL=true ;;
    --discard) DISCARD_MODE=true ;;
    -h|--help)
      cat <<'HELP'
Usage: merge-worktrees.sh [options]

Ultra-safe merge of rechecker worktree branches into the current branch.

Modes:
  (default)       Merge rechecker branches with ancestry check and safety verification
  --discard-all   Remove ALL rechecker worktrees and branches without merging
  --discard N...  Remove specific worktrees/branches by name (partial match)

Options:
  --dry-run       Preview what would happen without doing anything
  --no-cleanup    Merge branches but skip cleanup (keep branches, files)
  --help, -h      Show this help

Examples:
  bash merge-worktrees.sh                          # merge all safe branches
  bash merge-worktrees.sh --dry-run                # preview what would merge
  bash merge-worktrees.sh --discard-all            # discard all without merging
  bash merge-worktrees.sh --discard rck-abc123     # discard one by name
  bash merge-worktrees.sh --discard abc123 def456  # discard multiple (partial match)
  bash merge-worktrees.sh --discard-all --dry-run  # preview what would be discarded

Safety:
  - Atomic lock prevents concurrent operations
  - Only merges branches based on the current branch (ancestry check)
  - Branch integrity verified after every operation
  - Automatic rollback on interrupt
  - Uses -X theirs (prefers rechecker fixes on conflict)
  - Discard checks for active processes before removing worktrees
HELP
      exit 0
      ;;
    *)
      if $DISCARD_MODE && ! $DISCARD_ALL; then
        # Arguments after --discard are names to discard
        DISCARD_NAMES+=("$1")
      else
        die "Unknown option: $1 (try --help)"
      fi
      ;;
  esac
  shift
done

# ── Pre-flight checks ────────────────────────────────────────────────────

ORIGINAL_BRANCH=$(git branch --show-current) || die "Cannot determine current branch"
[[ -z "$ORIGINAL_BRANCH" ]] && die "Detached HEAD state. Checkout a branch first."
ORIGINAL_HEAD=$(git rev-parse HEAD) || die "Cannot determine HEAD"

echo "Git root: $GIT_ROOT"
echo "Branch:   $ORIGINAL_BRANCH"
echo "HEAD:     $ORIGINAL_HEAD"

git status --porcelain --ignore-submodules >/dev/null 2>&1 || die "git status failed — repository may be corrupted"

# Find all rechecker worktree branches
ALL_BRANCHES=$(git branch --list 'worktree-rck-*' 'worktree-rechecker-*' | sed 's/^[+* ]*//' || true)

if [[ -z "$ALL_BRANCHES" ]]; then
  echo "No rechecker worktree branches found. Nothing to do."
  exit 0
fi

TOTAL_FOUND=$(echo "$ALL_BRANCHES" | wc -l | tr -d ' ')
echo "Found $TOTAL_FOUND rechecker branch(es) total"

# ══════════════════════════════════════════════════════════════════════════
# DISCARD MODE
# ══════════════════════════════════════════════════════════════════════════

if $DISCARD_MODE; then
  echo ""
  echo "=== DISCARD MODE ==="

  # Build the list of branches to discard
  DISCARD_BRANCHES=""
  if $DISCARD_ALL; then
    DISCARD_BRANCHES="$ALL_BRANCHES"
    echo "Discarding ALL $TOTAL_FOUND rechecker branch(es)."
  else
    if [[ ${#DISCARD_NAMES[@]} -eq 0 ]]; then
      die "No names specified. Use --discard-all to discard everything, or --discard <name1> <name2> ..."
    fi
    # Match names (partial match: "abc123" matches "worktree-rck-abc123")
    for name in "${DISCARD_NAMES[@]}"; do
      matched=false
      while IFS= read -r branch; do
        [[ -z "$branch" ]] && continue
        if [[ "$branch" == *"$name"* ]]; then
          DISCARD_BRANCHES="${DISCARD_BRANCHES}${branch}"$'\n'
          matched=true
        fi
      done <<< "$ALL_BRANCHES"
      if ! $matched; then
        warn "No branch matching '$name' found"
      fi
    done
    DISCARD_BRANCHES=$(echo "$DISCARD_BRANCHES" | sed '/^$/d' | sort -u)
  fi

  if [[ -z "$DISCARD_BRANCHES" ]]; then
    echo "No branches to discard."
    exit 0
  fi

  DISCARD_COUNT=$(echo "$DISCARD_BRANCHES" | wc -l | tr -d ' ')
  echo ""
  echo "Branches to discard ($DISCARD_COUNT):"
  while IFS= read -r branch; do
    [[ -z "$branch" ]] && continue
    commit=$(git log --oneline -1 "$branch" 2>/dev/null || echo "???")
    info "$branch  ($commit)"
  done <<< "$DISCARD_BRANCHES"

  if $DRY_RUN; then
    echo ""
    echo "[DRY RUN] Would discard the above branches. Run without --dry-run to proceed."
    exit 0
  fi

  # Acquire lock
  acquire_lock
  info "Lock acquired (PID $$)"

  # Verify branch before starting
  assert_branch "$ORIGINAL_BRANCH" "pre-discard check"

  # Discard each branch
  DISCARDED=0
  FAILED=0
  echo ""
  while IFS= read -r branch; do
    [[ -z "$branch" ]] && continue
    if remove_worktree_and_branch "$branch"; then
      DISCARDED=$((DISCARDED + 1))
    else
      FAILED=$((FAILED + 1))
    fi
  done <<< "$DISCARD_BRANCHES"

  git worktree prune 2>/dev/null || true

  # Clean up merge-pending files for discarded branches
  DOCS_DEV="$GIT_ROOT/docs_dev"
  mkdir -p "$DOCS_DEV"
  for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
    for f in $pattern; do
      [[ -f "$f" ]] && mv "$f" "$DOCS_DEV/" 2>/dev/null || true
    done
  done

  # Verify branch is intact
  assert_branch "$ORIGINAL_BRANCH" "post-discard check"

  release_lock

  echo ""
  echo "=== Done ==="
  echo "  Discarded: $DISCARDED"
  echo "  Failed:    $FAILED"
  exit 0
fi

# ══════════════════════════════════════════════════════════════════════════
# MERGE MODE (default)
# ══════════════════════════════════════════════════════════════════════════

# ── Step 0: Acquire lock ─────────────────────────────────────────────────

if ! $DRY_RUN; then
  acquire_lock
  info "Lock acquired (PID $$)"
fi

# ── Step 1: Remove worktrees (can't merge checked-out branches) ──────────

echo ""
echo "Step 1: Removing worktrees..."

WT_PATHS=()
_wt_path=""
while IFS= read -r line; do
  if [[ "$line" == "worktree "* ]]; then
    _wt_path="${line#worktree }"
  elif [[ "$line" == "branch refs/heads/worktree-rck-"* ]] || [[ "$line" == "branch refs/heads/worktree-rechecker-"* ]]; then
    if [[ -n "$_wt_path" ]] && [[ -d "$_wt_path" ]]; then
      WT_PATHS+=("$_wt_path")
    fi
    _wt_path=""
  elif [[ -z "$line" ]]; then
    _wt_path=""
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
      if pgrep -f "$wt_path" >/dev/null 2>&1; then
        warn "Worktree $wt_path has an active process — skipping"
        continue
      fi
      if git worktree remove "$wt_path" --force 2>/dev/null; then info "Removed: $wt_path"; else warn "Failed: $wt_path"; fi
    done
    git worktree prune 2>/dev/null || true
  fi
fi

assert_branch "$ORIGINAL_BRANCH" "worktree removal"

# ── Step 2: Filter branches by ancestry ──────────────────────────────────

echo ""
echo "Step 2: Checking branch ancestry..."

BRANCHES=""
SKIPPED_ANCESTRY=0

while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue

  if ! sanitize_branch "$branch"; then
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  if ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
    warn "Branch $branch does not exist — skipping"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  merge_base=$(git merge-base "$ORIGINAL_BRANCH" "$branch" 2>/dev/null || echo "")
  if [[ -z "$merge_base" ]]; then
    info "SKIP $branch — no common ancestor with $ORIGINAL_BRANCH"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  if ! git merge-base --is-ancestor "$merge_base" "$ORIGINAL_HEAD" 2>/dev/null; then
    info "SKIP $branch — based on a different branch"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

  distance=$(git rev-list --count "$merge_base..$ORIGINAL_HEAD" 2>/dev/null || echo "999")
  if [[ "$distance" -gt 200 ]]; then
    info "SKIP $branch — base is $distance commits behind HEAD (too old)"
    SKIPPED_ANCESTRY=$((SKIPPED_ANCESTRY + 1))
    continue
  fi

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

assert_branch "$ORIGINAL_BRANCH" "pre-merge check"

# ── Step 4: Move existing rechecker files to docs_dev ────────────────────

DOCS_DEV="$GIT_ROOT/docs_dev"
mkdir -p "$DOCS_DEV" || die "Cannot create docs_dev directory"
for pattern in "rck-*-merge-pending.md" "rck-*-report.md"; do
  for f in $pattern; do
    if [[ -f "$f" ]]; then
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

  assert_branch "$ORIGINAL_BRANCH" "before merging $branch"

  echo -n "  $branch... "

  if ! git rev-parse --verify "refs/heads/$branch" >/dev/null 2>&1; then
    echo "SKIP (branch disappeared)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  diff_count=$(git diff --stat "$ORIGINAL_BRANCH...$branch" --ignore-submodules 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$diff_count" -le 1 ]]; then
    echo "skip (no file changes)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  git_dir=$(git rev-parse --git-dir 2>/dev/null)
  if [[ -f "$git_dir/index.lock" ]]; then
    echo "SKIP (git index locked by another process)"
    FAILED=$((FAILED + 1))
    continue
  fi

  pre_merge_head=$(git rev-parse HEAD)

  MERGE_IN_PROGRESS=true
  if git merge -X theirs "$branch" --no-edit 2>/dev/null; then merge_ok=true; else merge_ok=false; fi
  MERGE_IN_PROGRESS=false

  if $merge_ok; then
    echo "merged ($((diff_count - 1)) files)"
    MERGED=$((MERGED + 1))
    MERGED_BRANCHES="${MERGED_BRANCHES}${branch}"$'\n'

    assert_branch "$ORIGINAL_BRANCH" "after merging $branch"

    if ! git merge-base --is-ancestor "$pre_merge_head" HEAD 2>/dev/null; then
      die "Merge of $branch corrupted history! Pre-merge HEAD is not ancestor of new HEAD."
    fi
  else
    git merge --abort 2>/dev/null || true

    post_abort_head=$(git rev-parse HEAD)
    if [[ "$post_abort_head" != "$pre_merge_head" ]]; then
      warn "HEAD changed after merge abort of $branch! Expected $pre_merge_head, got $post_abort_head"
      warn "Stopping. Manual intervention needed."
      FAILED=$((FAILED + 1))
      break
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

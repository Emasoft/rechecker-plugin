#!/usr/bin/env bash
# review-loop.sh - Core review loop
# Uses 'claude --worktree' for automatic worktree lifecycle management.
# Claude Code creates worktrees at <project>/.claude/worktrees/<name>.
# Auto-cleans if no changes, keeps if changes were committed.
#
# KEY DESIGN: We do NOT pass a diff file to the worktree because untracked
# files in the main working directory are NOT visible in worktrees. Instead,
# the agent runs 'git diff' itself using the commit SHA (all git objects are
# shared across worktrees). The agent also resets its worktree to match the
# commit state with 'git reset --hard <SHA>' before reviewing.
set -eu
set -o pipefail 2>/dev/null || true

# ── Parameters ──────────────────────────────────────────────────
PROJECT_DIR="$1"
COMMIT_SHA="$2"
CURRENT_BRANCH="$3"
REPORTS_DIR="$4"
TIMESTAMP="$5"
PLUGIN_ROOT="$6"

MAX_PASSES=30
AGENT_FILE="${PLUGIN_ROOT}/agents/code-reviewer.md"
SCAN_SCRIPT="${PLUGIN_ROOT}/scripts/scan.sh"
CHANGED_FILES_SCRIPT="${PLUGIN_ROOT}/scripts/changed-files.sh"

# ── State tracking ──────────────────────────────────────────────
TOTAL_ISSUES_FOUND=0
TOTAL_ISSUES_FIXED=0
# Use newline as delimiter for pass summaries to avoid corrupting
# sentences that contain dots (filenames like auth.py, etc.)
PASS_SUMMARIES=""
add_summary() {
    if [ -n "$PASS_SUMMARIES" ]; then
        PASS_SUMMARIES="${PASS_SUMMARIES}
${1}"
    else
        PASS_SUMMARIES="$1"
    fi
}
FINAL_STATUS="unknown"
# Track consecutive no-fix passes to detect persistent agent failure
CONSECUTIVE_NO_FIX=0
MAX_CONSECUTIVE_NO_FIX=2
# PRE_MERGE_SHA = HEAD before a merge (used as diff base for pass N+1)
# REVIEW_TARGET_SHA = HEAD after a merge (used as reset target for pass N+1)
PRE_MERGE_SHA="${COMMIT_SHA}"
REVIEW_TARGET_SHA="${COMMIT_SHA}"

# ── Helper: clean up a Claude-managed worktree and its branch ───
cleanup_worktree() {
    local wt_name="$1"
    local wt_path="${PROJECT_DIR}/.claude/worktrees/${wt_name}"
    local branch_name="worktree-${wt_name}"
    cd "$PROJECT_DIR"
    # Prune stale worktree entries first
    git worktree prune 2>/dev/null || true
    # Try proper removal, then fallback to rm only if path is strictly under .claude/worktrees/
    if ! git worktree remove --force "$wt_path" 2>/dev/null; then
        if [ -d "$wt_path" ] && [[ "$wt_path" == "${PROJECT_DIR}/.claude/worktrees/"* ]]; then
            rm -rf "$wt_path" 2>/dev/null || true
        fi
    fi
    git branch -D "$branch_name" 2>/dev/null || true
}

# ── Main loop ───────────────────────────────────────────────────
for PASS_NUM in $(seq 1 "$MAX_PASSES"); do
    WT_NAME="rechecker-${TIMESTAMP}-pass${PASS_NUM}"
    WORKTREE_PATH="${PROJECT_DIR}/.claude/worktrees/${WT_NAME}"
    WT_BRANCH="worktree-${WT_NAME}"
    REPORT_FILENAME="rechecker_${TIMESTAMP}_pass${PASS_NUM}.md"
    REPORT_FILE="${REPORTS_DIR}/${REPORT_FILENAME}"

    # ── Clean up any leftover worktree from a previous failed run ─
    cleanup_worktree "$WT_NAME"

    # ── Check if there are changes to review ────────────────────
    cd "$PROJECT_DIR"
    COMMIT_MSG=""

    if [ "$PASS_NUM" -eq 1 ]; then
        # First pass: review the triggering commit
        COMMIT_MSG=$(git log -1 --format="%s" "${COMMIT_SHA}" 2>/dev/null || echo "Unknown")
        DIFF_STAT=$(git diff --stat "${COMMIT_SHA}~1..${COMMIT_SHA}" 2>/dev/null || \
                    git show --stat "${COMMIT_SHA}" --format="" 2>/dev/null || echo "")
        # The target for diff/reset/changed-files is the triggering commit
        PASS_TARGET_SHA="${COMMIT_SHA}"
    else
        # Subsequent passes: review what the previous merge introduced.
        # REVIEW_TARGET_SHA was set AFTER the merge in the previous pass
        # (= the post-merge HEAD), so it represents the NEW state including fixes.
        # We diff from pre-merge to post-merge to see exactly what was fixed.
        COMMIT_MSG="Rechecker pass $((PASS_NUM - 1)) fixes"
        DIFF_STAT=$(git diff --stat "${PRE_MERGE_SHA}..${REVIEW_TARGET_SHA}" 2>/dev/null || echo "")
        PASS_TARGET_SHA="${REVIEW_TARGET_SHA}"
    fi

    if [ -z "$DIFF_STAT" ]; then
        FINAL_STATUS="clean"
        add_summary "Pass ${PASS_NUM}: No changes to review"
        break
    fi

    # ── Build the prompt ────────────────────────────────────────
    # PASS_TARGET_SHA is the commit the agent should reset to and review.
    # For pass 1: the triggering commit.
    # For pass N>1: the post-merge HEAD from the previous pass (includes fixes).
    # Detect first commit (no parent) to avoid "bad revision SHA~1" error
    if [ "$PASS_NUM" -eq 1 ]; then
        if git rev-parse "${COMMIT_SHA}~1" >/dev/null 2>&1; then
            DIFF_COMMAND="git diff ${COMMIT_SHA}~1..${COMMIT_SHA}"
        else
            # First commit in repo: use git show to display all changes
            DIFF_COMMAND="git show --format='' ${COMMIT_SHA}"
        fi
    else
        DIFF_COMMAND="git diff ${PRE_MERGE_SHA}..${PASS_TARGET_SHA}"
    fi
    RESET_COMMAND="git reset --hard ${PASS_TARGET_SHA}"
    CHANGED_FILES_GEN="bash ${CHANGED_FILES_SCRIPT} ${PASS_TARGET_SHA} .rechecker_changed_files.txt"

    REVIEW_PROMPT="You are reviewing code in a git worktree. Follow these steps EXACTLY:

STEP 1: Run the automated linter and security scan with autofix.
  This is the FIRST thing you must do. It runs Super-Linter (40+ language linters),
  Semgrep (OWASP security rules with autofix), and TruffleHog (secret detection) via Docker.

  First, ensure the worktree has the right files checked out:
    ${RESET_COMMAND}

  Then generate the list of changed files and run the scan ONLY on those files.
  The scan report MUST go into a subdirectory (not the worktree root) to avoid
  polluting the worktree with untracked files that would be caught by git add -A.

    ${CHANGED_FILES_GEN}
    mkdir -p .rechecker_scan_output
    bash ${SCAN_SCRIPT} --autofix --target-list .rechecker_changed_files.txt --scan-timeout 10800 --skip-pull -o .rechecker_scan_output .

  The changed-files.sh helper generates a clean list (one path per line, excludes
  deleted files, handles first commits and merge commits). The --target-list flag
  tells scan.sh to scan only those files instead of the entire codebase.
  --scan-timeout 10800 allows up to 3 hours total for the scan.
  --skip-pull uses cached Docker images (avoids re-pulling on every pass).

  IMPORTANT: Do NOT run scan.sh without --target-list, as that would scan the
  entire codebase and autofix unrelated files.

  The script prints the scan report file path to stdout. Read that report file.
  After reading the report, clean up scan artifacts so they don't pollute the commit:
    rm -rf .rechecker_scan_output .rechecker_changed_files.txt

  If the scan fails (e.g. Docker not available, no changed files), just continue to STEP 2.
  The scan is a best-effort enhancement, not a hard requirement.
  If the scan auto-fixed files, note what was fixed. Those fixes are already applied in place.

STEP 2: View the code changes to review (the original commit diff):
  ${DIFF_COMMAND}

STEP 3: Review every changed file thoroughly using the checklist in your agent instructions.
  Also review any remaining (unfixed) findings from the scan report in STEP 1.

STEP 4: Fix any issues you find by editing the source files.
  Do NOT re-fix things the scan already auto-fixed in STEP 1.

STEP 5: If you made fixes (in STEP 4) OR if the scan made fixes (in STEP 1), commit everything:
  git add -A && git commit -m 'rechecker: pass ${PASS_NUM} fixes'

STEP 6: Write your review report to: ${REPORT_FILENAME}
  (Use the Write tool to save it in the current working directory.)
  Include a section for scan results (what the scan found, what it auto-fixed, what remains).

Context:
- Commit message: ${COMMIT_MSG}
- Commit SHA: ${COMMIT_SHA}
- Review pass: ${PASS_NUM} of ${MAX_PASSES}

If you find NO issues AND the scan found NO issues, do NOT create a commit. Just write the report with ISSUES_FOUND: 0"

    # ── Run headless Claude in a managed worktree ────────────────
    # Retry logic for transient API errors (rate limits, server overload).
    cd "$PROJECT_DIR"
    MAX_RETRIES=3
    RETRY_DELAY=30
    CLAUDE_EXIT_CODE=0

    for RETRY in $(seq 0 "$MAX_RETRIES"); do
        if [ "$RETRY" -gt 0 ]; then
            WAIT_TIME=$((RETRY_DELAY * RETRY))
            add_summary "Pass ${PASS_NUM}: API error, retry ${RETRY}/${MAX_RETRIES} after ${WAIT_TIME}s"
            sleep "$WAIT_TIME"
            cleanup_worktree "$WT_NAME"
            cd "$PROJECT_DIR"
        fi

        CLAUDE_STDERR_FILE="${PROJECT_DIR}/.rechecker_stderr_${TIMESTAMP}_pass${PASS_NUM}.log"
        claude --worktree "$WT_NAME" \
            --agent "$AGENT_FILE" \
            -p "$REVIEW_PROMPT" \
            --dangerously-skip-permissions \
            2>"$CLAUDE_STDERR_FILE"
        CLAUDE_EXIT_CODE=$?

        if [ "$CLAUDE_EXIT_CODE" -eq 0 ]; then
            break
        fi

        # Check stderr for transient errors worth retrying
        STDERR_CONTENT=$(cat "$CLAUDE_STDERR_FILE" 2>/dev/null || echo "")
        IS_TRANSIENT=false
        if echo "$STDERR_CONTENT" | grep -qiE "rate.?limit|429|too many requests|overloaded|503|502|504|server error|timeout|ECONNRESET|ETIMEDOUT|capacity"; then
            IS_TRANSIENT=true
        fi

        if [ "$IS_TRANSIENT" = "false" ]; then
            add_summary "Pass ${PASS_NUM}: API error (non-transient), skipping retries"
            break
        fi

        if [ "$RETRY" -eq "$MAX_RETRIES" ]; then
            add_summary "Pass ${PASS_NUM}: API error, max retries exhausted"
        fi
    done

    # Clean up stderr log
    rm -f "$CLAUDE_STDERR_FILE" 2>/dev/null || true

    # ── Retrieve report from worktree ───────────────────────────
    ISSUES_FOUND=-1
    ISSUES_FIXED=0
    REPORT_HAS_MARKER=false

    WT_REPORT_FILE="${WORKTREE_PATH}/${REPORT_FILENAME}"
    if [ -f "$WT_REPORT_FILE" ]; then
        cp "$WT_REPORT_FILE" "$REPORT_FILE"
    fi

    # Also copy scan reports from the subdirectory if they exist
    WT_SCAN_DIR="${WORKTREE_PATH}/.rechecker_scan_output"
    if [ -d "$WT_SCAN_DIR" ]; then
        for scan_file in "${WT_SCAN_DIR}"/*.json "${WT_SCAN_DIR}"/*.log; do
            if [ -f "$scan_file" ]; then
                cp "$scan_file" "${REPORTS_DIR}/" 2>/dev/null || true
            fi
        done
    fi

    if [ -f "$REPORT_FILE" ]; then
        FOUND_LINE=$(grep -i "^ISSUES_FOUND:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FOUND_LINE" ]; then
            REPORT_HAS_MARKER=true
            ISSUES_FOUND=$(echo "$FOUND_LINE" | grep -oE '[0-9]+' | head -1 || echo "")
            # If marker exists but has no numeric value, treat as 1 issue (safer than 0)
            if [ -z "$ISSUES_FOUND" ]; then
                ISSUES_FOUND=1
            fi
        fi

        FIXED_LINE=$(grep -i "^ISSUES_FIXED:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FIXED_LINE" ]; then
            ISSUES_FIXED=$(echo "$FIXED_LINE" | grep -oE '[0-9]+' | head -1 || echo "")
            if [ -z "$ISSUES_FIXED" ]; then
                ISSUES_FIXED=0
            fi
        fi

        # Validate extracted values are numeric (belt-and-suspenders)
        case "$ISSUES_FOUND" in
            ''|*[!0-9]*) ISSUES_FOUND=1 ;;
        esac
        case "$ISSUES_FIXED" in
            ''|*[!0-9]*) ISSUES_FIXED=0 ;;
        esac
    fi

    # If no valid report, treat as unknown issues (never assume clean).
    if [ "$REPORT_HAS_MARKER" = "false" ]; then
        if [ -d "$WORKTREE_PATH" ]; then
            cd "$WORKTREE_PATH" 2>/dev/null || cd "$PROJECT_DIR"
            WORKTREE_COMMITS=$(git log "${CURRENT_BRANCH}..${WT_BRANCH}" --oneline 2>/dev/null | wc -l | tr -d ' ')
            if [ "$WORKTREE_COMMITS" -gt 0 ] 2>/dev/null; then
                ISSUES_FOUND=1
                ISSUES_FIXED=1
            else
                ISSUES_FOUND=1
                ISSUES_FIXED=0
            fi
        else
            ISSUES_FOUND=1
            ISSUES_FIXED=0
        fi

        mkdir -p "$(dirname "$REPORT_FILE")"
        {
            echo "# Rechecker Review Report - Pass ${PASS_NUM}"
            echo ""
            echo "**Date**: $(date '+%Y-%m-%d %H:%M:%S')"
            echo "**Commit**: ${COMMIT_SHA:0:8}"
            echo ""
            echo "## Summary"
            echo "Review agent did not produce a valid report (missing ISSUES_FOUND marker)."
            echo ""
            echo "ISSUES_FOUND: ${ISSUES_FOUND}"
            echo "ISSUES_FIXED: ${ISSUES_FIXED}"
        } > "$REPORT_FILE"
    fi

    TOTAL_ISSUES_FOUND=$((TOTAL_ISSUES_FOUND + ISSUES_FOUND))
    TOTAL_ISSUES_FIXED=$((TOTAL_ISSUES_FIXED + ISSUES_FIXED))
    add_summary "Pass ${PASS_NUM}: ${ISSUES_FOUND} issues found, ${ISSUES_FIXED} fixed. Report: $(basename "$REPORT_FILE")"

    # ── If 0 issues found (from a valid report), we are done ────
    if [ "$ISSUES_FOUND" -eq 0 ] 2>/dev/null && [ "$REPORT_HAS_MARKER" = "true" ]; then
        FINAL_STATUS="clean"
        cleanup_worktree "$WT_NAME"
        break
    fi

    # ── Check if worktree still exists (= reviewer made changes) ─
    if [ ! -d "$WORKTREE_PATH" ]; then
        CONSECUTIVE_NO_FIX=$((CONSECUTIVE_NO_FIX + 1))

        if [ "$CONSECUTIVE_NO_FIX" -ge "$MAX_CONSECUTIVE_NO_FIX" ]; then
            FINAL_STATUS="agent_bug: reviewer found issues but failed to commit fixes ${CONSECUTIVE_NO_FIX} times in a row"
            add_summary "Pass ${PASS_NUM}: No fixes committed - ${CONSECUTIVE_NO_FIX} consecutive failures, giving up"
            break
        fi

        add_summary "Pass ${PASS_NUM}: No fixes committed by reviewer - retrying once more"
        if [ "$PASS_NUM" -eq "$MAX_PASSES" ]; then
            FINAL_STATUS="max_passes_reached"
        fi
        continue
    fi

    # ── Worktree exists: check for actual commits ───────────────
    cd "$WORKTREE_PATH" 2>/dev/null || cd "$PROJECT_DIR"
    WORKTREE_COMMITS=$(git log "${CURRENT_BRANCH}..${WT_BRANCH}" --oneline 2>/dev/null | wc -l | tr -d ' ')

    if [ "$WORKTREE_COMMITS" -eq 0 ] 2>/dev/null; then
        CONSECUTIVE_NO_FIX=$((CONSECUTIVE_NO_FIX + 1))
        cleanup_worktree "$WT_NAME"

        if [ "$CONSECUTIVE_NO_FIX" -ge "$MAX_CONSECUTIVE_NO_FIX" ]; then
            FINAL_STATUS="agent_bug: reviewer found issues but failed to commit fixes ${CONSECUTIVE_NO_FIX} times in a row"
            add_summary "Pass ${PASS_NUM}: No fixes committed - ${CONSECUTIVE_NO_FIX} consecutive failures, giving up"
            break
        fi

        add_summary "Pass ${PASS_NUM}: No fixes committed by reviewer - retrying once more"
        if [ "$PASS_NUM" -eq "$MAX_PASSES" ]; then
            FINAL_STATUS="max_passes_reached"
        fi
        continue
    fi

    # Reviewer committed fixes - reset the no-fix counter
    CONSECUTIVE_NO_FIX=0

    # ── Merge fixes from worktree branch back into main ─────────
    cd "$PROJECT_DIR"

    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        FINAL_STATUS="error: working directory not clean before merge at pass ${PASS_NUM}"
        add_summary "Pass ${PASS_NUM}: Merge skipped - dirty working directory"
        cleanup_worktree "$WT_NAME"
        break
    fi

    # Record HEAD before merge (diff base for the next pass)
    PRE_MERGE_SHA=$(git rev-parse HEAD)

    if git merge --no-edit "$WT_BRANCH" 2>/dev/null; then
        # Record POST-merge HEAD so the next pass reviews the right changes
        REVIEW_TARGET_SHA=$(git rev-parse HEAD)
        add_summary "Pass ${PASS_NUM}: Fixes merged successfully"
    else
        git merge --abort 2>/dev/null || true
        FINAL_STATUS="merge_conflict at pass ${PASS_NUM}"
        add_summary "Pass ${PASS_NUM}: MERGE CONFLICT - manual resolution needed"
        cleanup_worktree "$WT_NAME"
        break
    fi

    # ── Clean up this pass's worktree ───────────────────────────
    cleanup_worktree "$WT_NAME"

    if [ "$PASS_NUM" -eq "$MAX_PASSES" ]; then
        FINAL_STATUS="max_passes_reached"
    fi
done

# ── Write final summary report ──────────────────────────────────
SUMMARY_FILE="${REPORTS_DIR}/rechecker_${TIMESTAMP}_summary.md"
{
    echo "# Rechecker Summary"
    echo ""
    echo "**Date**: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "**Trigger commit**: ${COMMIT_SHA:0:8}"
    echo "**Branch**: ${CURRENT_BRANCH}"
    echo "**Status**: ${FINAL_STATUS}"
    echo "**Total issues found**: ${TOTAL_ISSUES_FOUND}"
    echo "**Total issues fixed**: ${TOTAL_ISSUES_FIXED}"
    echo ""
    echo "## Pass Details"
    # PASS_SUMMARIES uses newlines as delimiter (not dots)
    echo "$PASS_SUMMARIES" | while IFS= read -r line || [ -n "$line" ]; do
        if [ -n "$line" ]; then
            echo "- ${line}"
        fi
    done
    echo ""
    echo "## Report Files"
    for f in "${REPORTS_DIR}"/rechecker_${TIMESTAMP}_pass*.md; do
        if [ -f "$f" ]; then
            echo "- $(basename "$f")"
        fi
    done
} > "$SUMMARY_FILE"

# ── Output summary to stdout (captured by rechecker.sh) ────────
REPORT_INSTRUCTION="READ the summary report now: ${SUMMARY_FILE}"

if [ "$FINAL_STATUS" = "clean" ]; then
    echo "Review completed (${FINAL_STATUS}). ${TOTAL_ISSUES_FOUND} total issues found across all passes, ${TOTAL_ISSUES_FIXED} fixed. All code changes verified clean. ${REPORT_INSTRUCTION}"
elif echo "$FINAL_STATUS" | grep -q "agent_bug"; then
    echo "Review completed with AGENT BUG. ${TOTAL_ISSUES_FOUND} issues found but reviewer failed to commit fixes after retries. READ the per-pass reports and fix the issues yourself: ${REPORTS_DIR}/rechecker_${TIMESTAMP}_pass*.md -- then ${REPORT_INSTRUCTION}"
elif echo "$FINAL_STATUS" | grep -q "merge_conflict"; then
    echo "Review completed with MERGE CONFLICT. ${TOTAL_ISSUES_FOUND} issues found, ${TOTAL_ISSUES_FIXED} fixed before conflict. Manual merge resolution needed. ${REPORT_INSTRUCTION}"
elif [ "$FINAL_STATUS" = "max_passes_reached" ]; then
    echo "Review completed (max ${MAX_PASSES} passes reached). ${TOTAL_ISSUES_FOUND} total issues found, ${TOTAL_ISSUES_FIXED} fixed. Some issues may remain. READ the per-pass reports for remaining issues: ${REPORTS_DIR}/rechecker_${TIMESTAMP}_pass*.md -- then ${REPORT_INSTRUCTION}"
else
    echo "Review completed (${FINAL_STATUS}). ${TOTAL_ISSUES_FOUND} issues found, ${TOTAL_ISSUES_FIXED} fixed. ${REPORT_INSTRUCTION}"
fi

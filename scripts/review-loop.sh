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
set -euo pipefail

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
# Track the HEAD before each pass so we know what diff to review next
REVIEW_TARGET_SHA="${COMMIT_SHA}"

# ── Helper: clean up a Claude-managed worktree and its branch ───
cleanup_worktree() {
    local wt_name="$1"
    local wt_path="${PROJECT_DIR}/.claude/worktrees/${wt_name}"
    local branch_name="worktree-${wt_name}"
    cd "$PROJECT_DIR"
    git worktree remove --force "$wt_path" 2>/dev/null || rm -rf "$wt_path" 2>/dev/null || true
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
        # Verify the commit has actual changes (not empty)
        DIFF_STAT=$(git diff --stat "${COMMIT_SHA}~1..${COMMIT_SHA}" 2>/dev/null || \
                    git show --stat "${COMMIT_SHA}" --format="" 2>/dev/null || echo "")
    else
        # Subsequent passes: review what the previous merge introduced
        COMMIT_MSG="Rechecker pass $((PASS_NUM - 1)) fixes"
        DIFF_STAT=$(git diff --stat "${REVIEW_TARGET_SHA}..HEAD" 2>/dev/null || echo "")
        # Update the target SHA for git diff commands used by the agent
        REVIEW_TARGET_SHA=$(git rev-parse HEAD 2>/dev/null || echo "$COMMIT_SHA")
    fi

    if [ -z "$DIFF_STAT" ]; then
        FINAL_STATUS="clean"
        add_summary "Pass ${PASS_NUM}: No changes to review"
        break
    fi

    # ── Build the prompt ────────────────────────────────────────
    # The agent runs git commands itself to view the diff (no diff file needed).
    # It also resets the worktree to match the commit state so it can edit
    # the actual source files.
    if [ "$PASS_NUM" -eq 1 ]; then
        DIFF_COMMAND="git diff ${COMMIT_SHA}~1..${COMMIT_SHA}"
        RESET_COMMAND="git reset --hard ${COMMIT_SHA}"
    else
        DIFF_COMMAND="git diff ${REVIEW_TARGET_SHA}~1..${REVIEW_TARGET_SHA}"
        RESET_COMMAND="git reset --hard ${REVIEW_TARGET_SHA}"
    fi

    # Build the scan report filename for this pass
    SCAN_REPORT_FILENAME="rechecker_${TIMESTAMP}_pass${PASS_NUM}_scan.json"

    REVIEW_PROMPT="You are reviewing code in a git worktree. Follow these steps EXACTLY:

STEP 1: Reset the worktree to match the commit being reviewed:
  ${RESET_COMMAND}

STEP 2: Run the automated linter and security scan with autofix.
  This runs Super-Linter (40+ language linters), Semgrep (security), and TruffleHog (secrets).
  It auto-fixes style and security issues where supported.
  Run this command:
    bash ${SCAN_SCRIPT} --autofix -o . .
  The script prints the report file path to stdout. Read that report file.
  IMPORTANT: If the scan fails (e.g. Docker not available), just continue to STEP 3.
  The scan is a best-effort enhancement, not a hard requirement.
  If the scan auto-fixed files, note what was fixed. Those fixes are already applied.

STEP 3: View the code changes to review (the original commit diff):
  ${DIFF_COMMAND}

STEP 4: Review every changed file thoroughly using the checklist in your agent instructions.
  Also review any remaining (unfixed) findings from the scan report in STEP 2.

STEP 5: Fix any issues you find by editing the source files.
  Do NOT re-fix things the scan already auto-fixed in STEP 2.

STEP 6: If you made fixes (in STEP 5) OR if the scan made fixes (in STEP 2), commit everything:
  git add -A && git commit -m 'rechecker: pass ${PASS_NUM} fixes'

STEP 7: Write your review report to: ${REPORT_FILENAME}
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

        CLAUDE_STDERR_FILE="${PROJECT_DIR}/.rechecker_stderr.log"
        claude --worktree "$WT_NAME" \
            --agent "$AGENT_FILE" \
            -p "$REVIEW_PROMPT" \
            --permission-mode acceptEdits \
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

    # Also copy the scan report if it exists
    WT_SCAN_REPORT="${WORKTREE_PATH}/${SCAN_REPORT_FILENAME}"
    if [ -f "$WT_SCAN_REPORT" ]; then
        cp "$WT_SCAN_REPORT" "${REPORTS_DIR}/${SCAN_REPORT_FILENAME}"
    fi
    # scan.sh may have saved the report with its own timestamp name; grab any JSON reports
    for scan_json in "${WORKTREE_PATH}"/scan_report_*.json; do
        if [ -f "$scan_json" ]; then
            cp "$scan_json" "${REPORTS_DIR}/" 2>/dev/null || true
        fi
    done

    if [ -f "$REPORT_FILE" ]; then
        FOUND_LINE=$(grep -i "^ISSUES_FOUND:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FOUND_LINE" ]; then
            REPORT_HAS_MARKER=true
            ISSUES_FOUND=$(echo "$FOUND_LINE" | grep -oE '[0-9]+' | head -1 || echo "0")
            ISSUES_FOUND="${ISSUES_FOUND:-0}"
        fi

        FIXED_LINE=$(grep -i "^ISSUES_FIXED:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FIXED_LINE" ]; then
            ISSUES_FIXED=$(echo "$FIXED_LINE" | grep -oE '[0-9]+' | head -1 || echo "0")
            ISSUES_FIXED="${ISSUES_FIXED:-0}"
        fi
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

    # Record HEAD before merge for diffing in the next pass
    REVIEW_TARGET_SHA=$(git rev-parse HEAD)

    if git merge --no-edit "$WT_BRANCH" 2>/dev/null; then
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
    echo "$PASS_SUMMARIES" | while IFS= read -r line; do
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
REPORT_INSTRUCTION="READ the summary report now: ${SUMMARY_FILE} -- then use the report content as your next commit message (amend the previous commit with: git commit --amend -m '<report content>')."

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

#!/usr/bin/env bash
# review-loop.sh - Core review loop
# Creates worktrees, runs headless Claude for review, merges fixes, iterates
set -euo pipefail

# ── Parameters ──────────────────────────────────────────────────
PROJECT_DIR="$1"
COMMIT_SHA="$2"
CURRENT_BRANCH="$3"
REPORTS_DIR="$4"
TIMESTAMP="$5"
PLUGIN_ROOT="$6"

MAX_PASSES=5
WORKTREE_BASE="${PROJECT_DIR}/.rechecker/worktrees"
AGENT_FILE="${PLUGIN_ROOT}/agents/code-reviewer.md"

mkdir -p "$WORKTREE_BASE"

# ── State tracking ──────────────────────────────────────────────
TOTAL_ISSUES_FOUND=0
TOTAL_ISSUES_FIXED=0
PASS_SUMMARIES=""
FINAL_STATUS="unknown"
# Track consecutive no-fix passes to detect persistent agent failure
CONSECUTIVE_NO_FIX=0
MAX_CONSECUTIVE_NO_FIX=2
# Track the HEAD before each pass so we know what diff to review next
DIFF_BASE_SHA="${COMMIT_SHA}"

# ── Helper: clean up a worktree and its branch ──────────────────
cleanup_worktree() {
    local wt_path="$1"
    local branch_name="$2"
    cd "$PROJECT_DIR"
    git worktree remove --force "$wt_path" 2>/dev/null || rm -rf "$wt_path"
    git branch -D "$branch_name" 2>/dev/null || true
}

# ── Main loop ───────────────────────────────────────────────────
for PASS_NUM in $(seq 1 $MAX_PASSES); do
    BRANCH_NAME="rechecker-pass-${PASS_NUM}-${TIMESTAMP}"
    WORKTREE_PATH="${WORKTREE_BASE}/pass-${PASS_NUM}"
    REPORT_FILENAME="rechecker_${TIMESTAMP}_pass${PASS_NUM}.md"
    REPORT_FILE="${REPORTS_DIR}/${REPORT_FILENAME}"
    # The headless Claude runs sandboxed inside the worktree, so it must write
    # the report there first. We copy it to reports_dev/ after it exits.
    WT_REPORT_FILE="${WORKTREE_PATH}/${REPORT_FILENAME}"

    # ── Clean up any leftover worktree/branch at this path ──────
    if [ -d "$WORKTREE_PATH" ]; then
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
    fi
    cd "$PROJECT_DIR"
    git branch -D "$BRANCH_NAME" 2>/dev/null || true

    # ── Create worktree ─────────────────────────────────────────
    cd "$PROJECT_DIR"
    if ! git worktree add "$WORKTREE_PATH" -b "$BRANCH_NAME" HEAD 2>/dev/null; then
        FINAL_STATUS="error: failed to create worktree for pass ${PASS_NUM}"
        PASS_SUMMARIES="${PASS_SUMMARIES}Pass ${PASS_NUM}: ERROR - failed to create worktree. "
        break
    fi

    # ── Generate the diff to review ─────────────────────────────
    cd "$PROJECT_DIR"
    DIFF_CONTENT=""
    COMMIT_MSG=""

    if [ "$PASS_NUM" -eq 1 ]; then
        # First pass: diff of the triggering commit
        # Handle first commit in repo (no parent)
        DIFF_CONTENT=$(git diff "${COMMIT_SHA}~1..${COMMIT_SHA}" 2>/dev/null || \
                       git show "${COMMIT_SHA}" --format="" 2>/dev/null || \
                       echo "ERROR: Unable to generate diff for commit ${COMMIT_SHA}")
        COMMIT_MSG=$(git log -1 --format="%s" "${COMMIT_SHA}" 2>/dev/null || echo "Unknown")
    else
        # Subsequent passes: diff of what the previous merge introduced
        DIFF_CONTENT=$(git diff "${DIFF_BASE_SHA}..HEAD" 2>/dev/null || \
                       echo "ERROR: Unable to generate diff between ${DIFF_BASE_SHA} and HEAD")
        COMMIT_MSG="Rechecker pass $((PASS_NUM - 1)) fixes"
    fi

    # Skip if diff is empty (no actual code changes)
    if [ -z "$DIFF_CONTENT" ] || [ "$DIFF_CONTENT" = "" ]; then
        FINAL_STATUS="clean"
        PASS_SUMMARIES="${PASS_SUMMARIES}Pass ${PASS_NUM}: No changes to review. "
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
        break
    fi

    # Save diff to a file in the worktree for the review agent to read
    DIFF_FILE="${WORKTREE_PATH}/.rechecker_diff.patch"
    echo "$DIFF_CONTENT" > "$DIFF_FILE"

    # ── Build the prompt for headless Claude ────────────────────
    # Keep the prompt concise - the agent definition has the full instructions
    # Report path is INSIDE the worktree so the sandboxed Claude can write it
    REVIEW_PROMPT="Review the code changes in the diff file at: .rechecker_diff.patch

Commit message: ${COMMIT_MSG}
Commit SHA: ${COMMIT_SHA}
This is review pass ${PASS_NUM} of ${MAX_PASSES}.

Save your review report to: ${REPORT_FILENAME}
(Save it in the current working directory, which is: ${WORKTREE_PATH})

After fixing all issues, commit your changes with:
git add -A && git commit -m 'rechecker: pass ${PASS_NUM} fixes'

If you find no issues, do NOT create a commit - just write the report with ISSUES_FOUND: 0"

    # ── Run headless Claude with the code-reviewer agent ────────
    cd "$WORKTREE_PATH"

    # Run claude -p with retry logic for transient API errors (rate limits,
    # server overload, etc.). The headless process may fail due to 429/5xx
    # errors. We detect this from the exit code and stderr, then wait with
    # exponential backoff before retrying the same pass.
    MAX_RETRIES=3
    RETRY_DELAY=30
    CLAUDE_EXIT_CODE=0

    for RETRY in $(seq 0 $MAX_RETRIES); do
        if [ "$RETRY" -gt 0 ]; then
            WAIT_TIME=$((RETRY_DELAY * RETRY))
            PASS_SUMMARIES="${PASS_SUMMARIES}(API error on pass ${PASS_NUM}, retry ${RETRY}/${MAX_RETRIES} after ${WAIT_TIME}s.) "
            sleep "$WAIT_TIME"
            # Re-enter the worktree in case cd was lost
            cd "$WORKTREE_PATH"
        fi

        CLAUDE_STDERR_FILE="${WORKTREE_PATH}/.rechecker_stderr.log"
        claude --agent "$AGENT_FILE" \
            -p "$REVIEW_PROMPT" \
            --permission-mode acceptEdits \
            --no-session-persistence \
            2>"$CLAUDE_STDERR_FILE"
        CLAUDE_EXIT_CODE=$?

        # Exit code 0 = success, proceed to parse report
        if [ "$CLAUDE_EXIT_CODE" -eq 0 ]; then
            break
        fi

        # Check stderr for transient errors worth retrying
        STDERR_CONTENT=$(cat "$CLAUDE_STDERR_FILE" 2>/dev/null || echo "")
        IS_TRANSIENT=false

        # Match rate limit, server overload, timeout, and 5xx errors
        if echo "$STDERR_CONTENT" | grep -qiE "rate.?limit|429|too many requests|overloaded|503|502|504|server error|timeout|ECONNRESET|ETIMEDOUT|capacity"; then
            IS_TRANSIENT=true
        fi

        if [ "$IS_TRANSIENT" = "false" ]; then
            # Non-transient error (auth failure, invalid request, etc.) - do not retry
            PASS_SUMMARIES="${PASS_SUMMARIES}(API error on pass ${PASS_NUM}: non-transient, skipping retries.) "
            break
        fi

        # On last retry, just break (we'll handle the failure below)
        if [ "$RETRY" -eq "$MAX_RETRIES" ]; then
            PASS_SUMMARIES="${PASS_SUMMARIES}(API error on pass ${PASS_NUM}: max retries exhausted.) "
        fi
    done

    # ── Copy report from worktree to reports_dev/ ─────────────────
    # The headless Claude writes the report inside the worktree (sandbox).
    # We copy it to the main project's reports_dev/ so it persists after
    # the worktree is destroyed.
    ISSUES_FOUND=-1
    ISSUES_FIXED=0
    REPORT_HAS_MARKER=false

    if [ -f "$WT_REPORT_FILE" ]; then
        cp "$WT_REPORT_FILE" "$REPORT_FILE"
    fi

    if [ -f "$REPORT_FILE" ]; then
        # Extract ISSUES_FOUND: N from report
        FOUND_LINE=$(grep -i "^ISSUES_FOUND:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FOUND_LINE" ]; then
            REPORT_HAS_MARKER=true
            ISSUES_FOUND=$(echo "$FOUND_LINE" | grep -oE '[0-9]+' | head -1 || echo "0")
            ISSUES_FOUND="${ISSUES_FOUND:-0}"
        fi

        # Extract ISSUES_FIXED: N from report
        FIXED_LINE=$(grep -i "^ISSUES_FIXED:" "$REPORT_FILE" 2>/dev/null | tail -1 || echo "")
        if [ -n "$FIXED_LINE" ]; then
            ISSUES_FIXED=$(echo "$FIXED_LINE" | grep -oE '[0-9]+' | head -1 || echo "0")
            ISSUES_FIXED="${ISSUES_FIXED:-0}"
        fi
    fi

    # If no report or report has no ISSUES_FOUND marker, the agent failed.
    # Treat this as "unknown issues" - do NOT assume clean. Check if the
    # reviewer at least made commits (it may have fixed things without
    # writing a proper report).
    if [ "$REPORT_HAS_MARKER" = "false" ]; then
        cd "$WORKTREE_PATH" 2>/dev/null || cd "$PROJECT_DIR"
        WORKTREE_COMMITS=$(git log "${CURRENT_BRANCH}..${BRANCH_NAME}" --oneline 2>/dev/null | wc -l | tr -d ' ')

        if [ "$WORKTREE_COMMITS" -gt 0 ] 2>/dev/null; then
            # Agent made fixes but didn't write a proper report - assume issues existed
            ISSUES_FOUND=1
            ISSUES_FIXED=1
        else
            # Agent failed entirely (no report, no commits) - cannot determine state.
            # Write a failure report and let the loop continue to retry on next pass.
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
            echo "Agent commits in worktree: ${WORKTREE_COMMITS}"
            echo ""
            echo "ISSUES_FOUND: ${ISSUES_FOUND}"
            echo "ISSUES_FIXED: ${ISSUES_FIXED}"
        } > "$REPORT_FILE"
    fi

    TOTAL_ISSUES_FOUND=$((TOTAL_ISSUES_FOUND + ISSUES_FOUND))
    TOTAL_ISSUES_FIXED=$((TOTAL_ISSUES_FIXED + ISSUES_FIXED))
    PASS_SUMMARIES="${PASS_SUMMARIES}Pass ${PASS_NUM}: ${ISSUES_FOUND} issues found, ${ISSUES_FIXED} fixed. Report: $(basename "$REPORT_FILE"). "

    # ── If 0 issues found (from a valid report), we are done ────
    if [ "$ISSUES_FOUND" -eq 0 ] 2>/dev/null && [ "$REPORT_HAS_MARKER" = "true" ]; then
        FINAL_STATUS="clean"
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
        break
    fi

    # ── Check if reviewer committed fixes in the worktree ───────
    cd "$WORKTREE_PATH" 2>/dev/null || cd "$PROJECT_DIR"
    WORKTREE_COMMITS=$(git log "${CURRENT_BRANCH}..${BRANCH_NAME}" --oneline 2>/dev/null | wc -l | tr -d ' ')

    if [ "$WORKTREE_COMMITS" -eq 0 ] 2>/dev/null; then
        # Reviewer found issues but did not commit fixes.
        CONSECUTIVE_NO_FIX=$((CONSECUTIVE_NO_FIX + 1))
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"

        if [ "$CONSECUTIVE_NO_FIX" -ge "$MAX_CONSECUTIVE_NO_FIX" ]; then
            # Two consecutive passes where the reviewer found issues but
            # failed to commit fixes. This is a persistent agent failure.
            FINAL_STATUS="agent_bug: reviewer found issues but failed to commit fixes ${CONSECUTIVE_NO_FIX} times in a row"
            PASS_SUMMARIES="${PASS_SUMMARIES}(No fixes committed - ${CONSECUTIVE_NO_FIX} consecutive failures, giving up.) "
            break
        fi

        # First failure: retry on the next pass with a fresh worktree
        PASS_SUMMARIES="${PASS_SUMMARIES}(No fixes committed by reviewer - retrying once more.) "
        if [ "$PASS_NUM" -eq "$MAX_PASSES" ]; then
            FINAL_STATUS="max_passes_reached"
        fi
        continue
    fi

    # Reviewer committed fixes successfully - reset the no-fix counter
    CONSECUTIVE_NO_FIX=0

    # ── Merge fixes from worktree branch back into main ─────────
    cd "$PROJECT_DIR"

    # Verify working directory is clean before merging
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        FINAL_STATUS="error: working directory not clean before merge at pass ${PASS_NUM}"
        PASS_SUMMARIES="${PASS_SUMMARIES}(Merge skipped: dirty working directory.) "
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
        break
    fi

    # Record HEAD before merge so we can diff against it in the next pass
    DIFF_BASE_SHA=$(git rev-parse HEAD)

    # Attempt the merge
    if git merge --no-edit "$BRANCH_NAME" 2>/dev/null; then
        PASS_SUMMARIES="${PASS_SUMMARIES}Fixes merged successfully. "
    else
        # Merge conflict - abort and report
        git merge --abort 2>/dev/null || true
        FINAL_STATUS="merge_conflict at pass ${PASS_NUM}"
        PASS_SUMMARIES="${PASS_SUMMARIES}MERGE CONFLICT - manual resolution needed. "
        cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
        break
    fi

    # ── Clean up this pass's worktree ───────────────────────────
    cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"

    # If this was the last allowed pass
    if [ "$PASS_NUM" -eq "$MAX_PASSES" ]; then
        FINAL_STATUS="max_passes_reached"
    fi
done

# ── Clean up worktree base directory if empty ───────────────────
rmdir "$WORKTREE_BASE" 2>/dev/null || true
rmdir "${PROJECT_DIR}/.rechecker/worktrees" 2>/dev/null || true

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
    # Split PASS_SUMMARIES by ". " and output as list items
    echo "$PASS_SUMMARIES" | tr '.' '\n' | while read -r line; do
        line=$(echo "$line" | sed 's/^[[:space:]]*//')
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
# This text becomes the additionalContext that gets injected into the main
# Claude session. It MUST instruct Claude to read the summary report so that
# Claude actually sees the detailed findings.
REPORT_INSTRUCTION="READ the summary report now: ${SUMMARY_FILE} -- then use the report content as your next commit message (amend the previous commit with: git commit --amend -m '<report content>')."

if [ "$FINAL_STATUS" = "clean" ]; then
    echo "Review completed (${FINAL_STATUS}). ${TOTAL_ISSUES_FOUND} total issues found across all passes, ${TOTAL_ISSUES_FIXED} fixed. All code changes verified clean. ${REPORT_INSTRUCTION}"
elif [ "$FINAL_STATUS" = "issues_reported_not_fixed" ]; then
    echo "Review completed (${FINAL_STATUS}). ${TOTAL_ISSUES_FOUND} issues found but reviewer did not commit fixes. READ the per-pass reports and fix the issues yourself: ${REPORTS_DIR}/rechecker_${TIMESTAMP}_pass*.md -- then ${REPORT_INSTRUCTION}"
elif echo "$FINAL_STATUS" | grep -q "merge_conflict"; then
    echo "Review completed with MERGE CONFLICT. ${TOTAL_ISSUES_FOUND} issues found, ${TOTAL_ISSUES_FIXED} fixed before conflict. Manual merge resolution needed. ${REPORT_INSTRUCTION}"
elif [ "$FINAL_STATUS" = "max_passes_reached" ]; then
    echo "Review completed (max ${MAX_PASSES} passes reached). ${TOTAL_ISSUES_FOUND} total issues found, ${TOTAL_ISSUES_FIXED} fixed. Some issues may remain. READ the per-pass reports for remaining issues: ${REPORTS_DIR}/rechecker_${TIMESTAMP}_pass*.md -- then ${REPORT_INSTRUCTION}"
else
    echo "Review completed (${FINAL_STATUS}). ${TOTAL_ISSUES_FOUND} issues found, ${TOTAL_ISSUES_FIXED} fixed. ${REPORT_INSTRUCTION}"
fi

---
name: recheck
description: >
  On-demand code review for any commit. Runs the same automated review loop
  that the PostToolUse hook triggers after git commits. Use when you want to
  manually review a specific commit or re-check the latest changes.
---

# Recheck - On-Demand Code Review

## Overview

Trigger the rechecker review loop manually on the latest commit (or a specified commit). This does the same thing the PostToolUse hook does automatically after git commits, but on demand.

## Prerequisites

- `claude` CLI on PATH (runs the review agent in headless mode)
- `python3` on PATH
- Git repository with at least one commit
- Docker (optional, for scan.sh security scanning)

## Instructions

1. Parse the user's request for an optional commit SHA
2. Run the recheck script via the Bash tool:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recheck.py" <COMMIT_SHA_OR_EMPTY>
   ```
3. Replace `<COMMIT_SHA_OR_EMPTY>` with the user-provided commit SHA, or omit it entirely to review HEAD
4. Wait for the script to complete (may take several minutes for large diffs)
5. Read the summary report file mentioned in the output to get the full results

## Output

- Per-pass review reports: `reports_dev/rechecker_<ts>_pass<N>.md`
- Final summary: `reports_dev/rechecker_<ts>_summary.md`
- Scan results: `reports_dev/scan-report-*.json` (if Docker available)
- Exit code 0 on success, non-zero on failure

## Error Handling

| Error | Resolution |
|-------|------------|
| Lock file exists | Another review is running — wait or check for stale lock |
| Docker not available | Scan step skipped, manual review continues |
| Worktree creation fails | Check git state, ensure no conflicts |
| Agent timeout (24h) | Review too complex — check for infinite loops in fixes |
| Rate limit (429) | Auto-retries 3x with 30/60/90s backoff |

## Examples

```
/recheck              # Review the latest commit on the current branch
/recheck abc1234      # Review a specific commit
/recheck HEAD~3       # Review 3 commits ago
```

## Resources

- Agent definition: `agents/code-reviewer.md`
- Hook entry point: `scripts/rechecker.py`
- Core review loop: `scripts/review-loop.py`
- Security scanner: `scripts/scan.sh`

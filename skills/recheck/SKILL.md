---
name: recheck
description: >
  Trigger with /recheck. On-demand two-phase code review for any commit.
  Same automated review loop as the PostToolUse hook but triggered manually.
---

# Recheck - On-Demand Code Review

## Overview

Trigger the rechecker review loop manually on the latest commit (or a specified commit). Runs the same two-phase pipeline (code review + functionality review) that fires automatically after git commits.

## Prerequisites

- `claude` CLI on PATH (runs the review agent in headless mode)
- `python3` on PATH
- Git repository with at least one commit
## Instructions

- [ ] Parse the user's request for an optional commit SHA
- [ ] Run the recheck script via the Bash tool:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recheck.py" <COMMIT_SHA_OR_EMPTY>
  ```
  Replace `<COMMIT_SHA_OR_EMPTY>` with the user-provided commit SHA, or omit it entirely to review HEAD
- [ ] Wait for the script to complete (may take several minutes for large diffs)
- [ ] Read the summary report file mentioned in the output to get the full results

## Output

- Per-pass review reports: `reports_dev/rechecker_<ts>_pass<N>.md`
- Final summary: `reports_dev/rechecker_<ts>_summary.md`
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

- Phase 1 agent: `agents/code-reviewer.md`
- Phase 2 agent: `agents/functionality-reviewer.md`
- Hook entry point: `scripts/rechecker.py`
- Core review loop: `scripts/review-loop.py`

---
name: recheck
description: >
  Trigger with /recheck. Use when you want to manually review code changes.
  Launches the 4-agent review pipeline in a worktree: lint, code review,
  functionality review, final lint — all with parallel subagent swarms.
---

# Recheck - On-Demand Code Review

## Overview

Trigger the rechecker pipeline manually. Without args, scans for all git repos with commits in the last 24h and reviews each. With a commit SHA, reviews that specific commit.

Uses 4 agents: recheck-orchestrator (opus), opus-code-reviewer, opus-functionality-reviewer, sonnet-code-fixer.

## Prerequisites

- `claude` CLI on PATH
- `python3` on PATH
- Git repository with at least one commit

## Instructions

1. [ ] Parse the user's request for an optional commit SHA
2. [ ] Run the recheck script via the Bash tool:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recheck.py" <COMMIT_SHA_OR_EMPTY> &
   ```
   Replace `<COMMIT_SHA_OR_EMPTY>` with the user-provided commit SHA, or omit it entirely to scan all repos with recent commits.
   The `&` forks the review to background so you can continue working.
3. [ ] The review runs asynchronously. Reports will appear in `reports_dev/` when complete.

## Output

- Combined review report in the worktree (committed by the orchestrator)
- Claude Code auto-merges the worktree back to main when the orchestrator exits

## Error Handling

| Error | Resolution |
|-------|------------|
| Worktree creation fails | Check git state, ensure no conflicts |
| Agent timeout (24h) | Review too complex — check for infinite loops in fixes |
| Rate limit (429) | Claude Code handles retries automatically |

## Examples

```
/recheck              # Scan all repos with commits in last 24h
/recheck abc1234      # Review a specific commit
/recheck HEAD~3       # Review 3 commits ago
```

## Resources

- Orchestrator: `agents/recheck-orchestrator.md`
- Code reviewer: `agents/opus-code-reviewer.md`
- Functionality reviewer: `agents/opus-functionality-reviewer.md`
- Code fixer: `agents/sonnet-code-fixer.md`
- Hook entry point: `scripts/rechecker.py`
- Skill entry point: `scripts/recheck.py`

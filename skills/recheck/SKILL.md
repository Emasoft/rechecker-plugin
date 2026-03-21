---
name: recheck
description: recheck the last committed changes
model: opus[1m]
---

Use this skill to trigger a full automated code review of the latest committed changes.

The pipeline uses 4 agents in a named worktree:
- **RO** (rechecker-orchestrator): Opus orchestrator — runs all 4 loops, makes 1 commit
- **OCR** (opus-code-reviewer): Opus swarm worker — finds correctness bugs
- **OFR** (opus-functionality-reviewer): Opus swarm worker — checks intent vs reality
- **SCF** (sonnet-code-fixer): Sonnet swarm worker — applies fixes

Flow (1 worktree, 1 commit at the end):
1. Loop 1: lint → sonnet fixes → repeat until 0 lint issues
2. Loop 2: opus finds bugs → sonnet fixes → repeat until 0 bugs
3. Loop 3: opus checks intent → sonnet fixes → repeat until 0 intent issues
4. Loop 4: final lint → sonnet fixes → repeat until 0 lint issues
5. Merge reports into single output
6. Commit → exit → Claude Code merges worktree

Copy the following checklist and use it to track the progress and completion of your tasks:

- [ ] Identify the latest commit SHA and the list of changed files
- [ ] Spawn the rechecker-orchestrator agent using the Agent tool with `subagent_type: "rechecker-orchestrator"`. The orchestrator has `isolation: worktree` so it will run in a worktree automatically. Wait for it to complete.
- [ ] After the orchestrator exits and the worktree is merged, move the report to reports_dev/:
  ```bash
  mkdir -p reports_dev && mv rechecker-report-*.md reports_dev/ 2>/dev/null; true
  ```
- [ ] Tell the user the report path: `reports_dev/rechecker-report-{TIMESTAMP}.md`

Do not consider the task done until all check points above have been completed.

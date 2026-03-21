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

- [ ] **Pre-check: find git root**. Run `git rev-parse --show-toplevel` via Bash. If it fails, tell the user "Not in a git repository — rechecker requires git" and STOP. Save the output as `GIT_ROOT` — all subsequent commands must run from this directory.
- [ ] Identify the latest commit SHA and changed files (run from `GIT_ROOT`):
  ```bash
  cd "<GIT_ROOT>" && git log -1 --format=%H && git show --name-only --format= --diff-filter=d HEAD
  ```
- [ ] **Launch the orchestrator in a worktree** by running via Bash (note the `cd` to `GIT_ROOT`):
  ```bash
  cd "<GIT_ROOT>" && claude --worktree rechecker-review \
    --agent "${CLAUDE_PLUGIN_ROOT}/agents/rechecker-orchestrator.md" \
    --dangerously-skip-permissions
  ```
  Replace `<GIT_ROOT>` with the actual path from the pre-check. The `cd` is critical — `claude --worktree` must run from the git root. Wait for it to complete.
- [ ] After the orchestrator exits and the worktree is merged, move the report to reports_dev/ (from `GIT_ROOT`):
  ```bash
  cd "<GIT_ROOT>" && mkdir -p reports_dev && mv rechecker-report-*.md reports_dev/ 2>/dev/null; true
  ```
- [ ] Tell the user the report path: `<GIT_ROOT>/reports_dev/rechecker-report-{TIMESTAMP}.md`

Do not consider the task done until all check points above have been completed.

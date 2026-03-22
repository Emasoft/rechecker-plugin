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

**This skill runs synchronously.** You are responsible for merging the worktree branch, resolving any conflicts, copying the report, and cleaning up. Do NOT leave unmerged branches behind.

## Naming convention

All files use the prefix `rck-{YYYYMMDD_HHMMSS}_{UUID6}`:
- Worktree name: `rck-20260321_193000_a1b2c3`
- Branch: `worktree-rck-20260321_193000_a1b2c3`
- Report: `rck-20260321_193000_a1b2c3-report.md`

Generate the tag at the start and use it throughout:
```bash
RCK_TAG="rck-$(date +%Y%m%d_%H%M%S)_$(head -c3 /dev/urandom | xxd -p | head -c6)"
```

## Checklist

Copy and use this checklist to track progress:

- [ ] **Pre-check: verify git repo**:
  ```bash
  git rev-parse --show-toplevel
  ```
  If it fails, tell the user "Not in a git repository — rechecker requires git" and STOP.
- [ ] **Generate the tag** and store it (you will use it in every subsequent step):
  ```bash
  RCK_TAG="rck-$(date +%Y%m%d_%H%M%S)_$(head -c3 /dev/urandom | xxd -p | head -c6)"
  echo "$RCK_TAG"
  ```
- [ ] Identify the latest commit SHA and changed files:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && git log -1 --format=%H && git show --name-only --format= --diff-filter=d HEAD
  ```
- [ ] **Launch the orchestrator in a worktree** using the tag as the worktree name:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && claude --worktree "$RCK_TAG" \
    --agent "rechecker-plugin:rechecker-orchestrator" \
    --dangerously-skip-permissions \
    -p "Run the full recheck pipeline on the latest commit."
  ```
  Wait for it to complete.
- [ ] **Copy the report** from the worktree to `reports_dev/` with the tag prefix:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && mkdir -p reports_dev && \
    cp .claude/worktrees/$RCK_TAG/rck-*-report.md "reports_dev/${RCK_TAG}-report.md" 2>/dev/null; \
    cp .claude/worktrees/$RCK_TAG/reports_dev/rck-*-report.md "reports_dev/${RCK_TAG}-report.md" 2>/dev/null; true
  ```
- [ ] **Merge the worktree branch** into the current branch:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && git merge "worktree-$RCK_TAG" --no-edit
  ```
  If there are merge conflicts, **resolve them yourself**: read the conflicting files, choose the correct resolution, stage, and commit. Do not leave conflicts unresolved.
- [ ] **Clean up** — move remaining reports, delete notice files:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && mv rck-*-report.md reports_dev/ 2>/dev/null; rm -f rck-*-merge-pending.md; true
  ```
- [ ] Tell the user the report path: `reports_dev/${RCK_TAG}-report.md`

Do not consider the task done until all check points above have been completed.

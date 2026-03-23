---
name: recheck
description: recheck the last committed changes
model: sonnet
---

Use this skill to trigger a full automated code review of the latest committed changes.

The pipeline uses a sonnet orchestrator in a named worktree:
- **RO** (rechecker-orchestrator): Sonnet orchestrator — runs all 4 loops, makes 1 commit
- **LLM Externalizer**: External LLM (grok/gemini via OpenRouter) — reviews code for bugs and intent mismatches
- **SCF** (sonnet-code-fixer): Sonnet swarm worker — applies fixes

**This skill runs synchronously.** You are responsible for merging the worktree branch after completion.

## Naming convention

Worktree names use a 6-char UUID: `rck-{UUID6}`
- Worktree name: `rck-a1b2c3`
- Branch: `worktree-rck-a1b2c3`
- Report: `rck-{YYYYMMDD_HHMMSS}_{UUID6}-report.md`

## Checklist

Copy and use this checklist to track progress:

- [ ] **Pre-check: verify git repo**:
  ```bash
  git rev-parse --show-toplevel
  ```
  If it fails, tell the user "Not in a git repository — rechecker requires git" and STOP.
- [ ] **Generate the worktree name**:
  ```bash
  RCK_UID=$(head -c3 /dev/urandom | xxd -p | head -c6)
  RCK_WT="rck-${RCK_UID}"
  echo "$RCK_WT"
  ```
- [ ] Identify the latest commit SHA and changed files:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && git log -1 --format=%H && git show --name-only --format= --diff-filter=d HEAD
  ```
- [ ] **Launch the orchestrator in a worktree**:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && claude --worktree "$RCK_WT" \
    --agent "rechecker-plugin:rechecker-orchestrator" \
    --dangerously-skip-permissions \
    -p "Run the full recheck pipeline on the latest commit."
  ```
  Wait for it to complete.
- [ ] **Merge all rechecker worktrees** using the bundled merge script:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && bash .rechecker/merge-worktrees.sh
  ```
  The script handles everything: merge with `-X ours`, move reports to `docs_dev/`, delete branches, clean up pending files.
  If `.rechecker/merge-worktrees.sh` doesn't exist, use the plugin copy:
  ```bash
  bash "${CLAUDE_PLUGIN_ROOT}/scripts/merge-worktrees.sh"
  ```
- [ ] Tell the user the report is in `docs_dev/` and the merge is complete.

Do not consider the task done until all check points above have been completed.

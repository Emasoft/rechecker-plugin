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
- [ ] **Deploy the merge script** to `.rechecker/` (rechecker.py does this in auto mode, but the skill must do it manually):
  ```bash
  cd "$(git rev-parse --show-toplevel)" && mkdir -p .rechecker && cp "${CLAUDE_PLUGIN_ROOT}/scripts/merge-worktrees.sh" .rechecker/merge-worktrees.sh && chmod +x .rechecker/merge-worktrees.sh
  ```
- [ ] **Ensure TLDR artifacts are gitignored**:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && for p in ".tldr/" ".tldrignore" ".tldr_session_*"; do grep -qxF "$p" .gitignore 2>/dev/null || echo "$p" >> .gitignore; done
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
- [ ] **Copy the report** from the worktree before merging (merge removes the worktree):
  ```bash
  cd "$(git rev-parse --show-toplevel)" && mkdir -p reports_dev && \
    cp ".claude/worktrees/$RCK_WT"/rck-*-report.md reports_dev/ 2>/dev/null; \
    cp ".claude/worktrees/$RCK_WT"/reports_dev/rck-*-report.md reports_dev/ 2>/dev/null; true
  ```
- [ ] **Merge the worktree branch** using the merge script:
  ```bash
  cd "$(git rev-parse --show-toplevel)" && bash .rechecker/merge-worktrees.sh
  ```
  The script handles: merge with `-X ours`, move reports to `docs_dev/`, delete branches, clean up.
- [ ] **Read the report** and tell the user what was found and fixed:
  ```bash
  ls -t reports_dev/rck-*-report.md | head -1
  ```
  Read that file and summarize the findings for the user.

Do not consider the task done until all checkpoints above have been completed.

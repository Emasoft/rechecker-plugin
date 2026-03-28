---
name: recheck
description: >
  Use when reviewing and fixing the last committed code changes.
  Trigger with /recheck or after a git commit when the recheck-after-commit rule is active.
---

## Overview

Automated code review and fix pipeline for the latest commit. Runs lint + 3 review passes + 1 conditional security pass, then commits fixes.

## Prerequisites

Copy this checklist and track your progress:

- [ ] Git repository with at least one commit
- [ ] `${CLAUDE_PLUGIN_ROOT}` set (plugin installed)
- [ ] LLM Externalizer MCP available (for review passes)

## Instructions

1. **Recursion guard** — check if the latest commit is already a rechecker commit:
   ```bash
   git log -1 --format=%s | grep -q '\[rechecker: skip\]' && echo "SKIP" || echo "PROCEED"
   ```
   If `SKIP`, stop immediately.

2. **Identify changed files** — get the list of files changed in the last commit:
   ```bash
   git show --name-only --format= --diff-filter=d HEAD
   ```
   Skip: media, binary, fonts, data blobs, generated files, lock files, files >500KB. Split remaining into **normal** (<=250KB, LLM Externalizer) and **large** (>250KB, opus agent).

3. **Setup session** — generate IDs, create report folder, take token snapshot:
   ```bash
   RCK_UUID=$(python3 -c "import uuid; print(uuid.uuid4().hex[:12])") && RCK_START_TS=$(date -u +%Y-%m-%dT%H:%M:%S) && RCK_COMMIT=$(git rev-parse HEAD) && REPORT_DIR="reports_dev/rck-${RCK_UUID}" && mkdir -p "$REPORT_DIR" && echo "RCK_UUID=$RCK_UUID RCK_START_TS=$RCK_START_TS RCK_COMMIT=$RCK_COMMIT REPORT_DIR=$REPORT_DIR"
   ```
   Token calibration: read a small file (e.g. `.claude-plugin/plugin.json`), then:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/count-tokens.py" --snapshot "$REPORT_DIR/before-tokens.json"
   ```

4. **Lint pass** — run linters per file type (see [lint-commands](lint-commands.md) for commands), save to `$REPORT_DIR/pass0-lint-raw.txt`. Spawn `rechecker-plugin:lint-filter` agent to extract errors only. If errors found, spawn `rechecker-plugin:sonnet-code-fixer` to fix them.

5. **Review passes (1-4)** — for each pass, review via LLM Externalizer (normal files) or opus agent (large files). See [review-passes](review-passes.md) for detailed instructions per pass. Pass 1: correctness. Pass 2: functional. Pass 3: adversarial. Pass 4: security (conditional — only if files touch auth/network/crypto/input). After each pass with issues, spawn `rechecker-plugin:sonnet-code-fixer` and wait for completion.

6. **Commit fixes** — if any files were changed, stage only fixed files and commit:
   ```bash
   git add <fixed-files>
   git commit -m "fix: apply rechecker fixes [rechecker: skip]"
   ```

7. **Finalize session** — run the finalize script:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/finalize-session.py" \
       --uuid "$RCK_UUID" --commit "$RCK_COMMIT" --start "$RCK_START_TS" \
       --report-dir "$REPORT_DIR" --snapshot "$REPORT_DIR/before-tokens.json" \
       --files-reviewed <N> --issues-found <N> --issues-fixed <N> [--commit-made]
   ```

## Output

Print a concise report:
```
--- Recheck: <UUID> (commit <hash>) ---
Files: <N> reviewed | Lint: <status> | Security: <skipped/triggered>
Pass 1-3: <N issues fixed / clean>
Commit: <yes (hash) / no fixes needed>
Tokens: <total> (input/output/cache breakdown)
Reports: .rechecker/reports/<UUID>/
---
```

## Error Handling

All stages fail-fast. If any linter, review, or fix step fails, the pipeline aborts and reports which step failed. No partial commits are made.

## Examples

```bash
# User commits code, then runs recheck
/recheck
```

## Resources

- [lint-commands](lint-commands.md) — linter commands per file type
  - Overview
  - Commands
- [review-passes](review-passes.md) — review instructions per pass
  - Shared Rules
  - Large File Instructions
  - Pass 1 — Code correctness
  - Pass 2 — Functional correctness
  - Pass 3 — Adversarial review
  - Pass 4 — Security audit

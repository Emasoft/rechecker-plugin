---
name: recheck
description: >
  Use when reviewing and fixing the last committed code changes.
  Trigger with /recheck or after a git commit when the recheck-after-commit rule is active.
---

## Overview

Automated code review and fix pipeline. A triage script handles all mechanical work and outputs a compact manifest with pre-split file groups. The orchestrator only reads group metadata and dispatches agents — never individual file paths.

## Prerequisites

Copy this checklist and track your progress:

- [ ] Git repository with at least one commit
- [ ] `${CLAUDE_PLUGIN_ROOT}` set (plugin installed)
- [ ] LLM Externalizer MCP available (for review passes)

## Instructions

1. **Run triage** — detects files, lints, classifies, splits into groups, outputs manifest:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py"
   ```
   Exit 3 = skip (recursion guard or no files). Exit 0 = read JSON manifest from stdout. The manifest contains `session`, `groups[]`, `lint`, and `security_pass`. Each group has: `id`, `group_file` (path to JSON with file list), `report_file`, `fixes_file`, `lint_errors_file`.

2. **Fix lint errors** — for each group where `lint_errors_file` is not null, spawn `rechecker-plugin:sonnet-code-fixer` with the group's `lint_errors_file` and `group_file`. The agent reads only its own group's files and errors.

3. **Review passes** — for each pass (see [review-passes](review-passes.md)), dispatch agents per group. For `category: "normal"` groups: send `group_file` path to LLM Externalizer `code_task` (the agent reads the group JSON to get file paths). For `category: "large"` groups: spawn opus agent with `group_file`. Each agent writes findings to its `report_file`. After each pass, for groups with issues, spawn `rechecker-plugin:sonnet-code-fixer` with `group_file` + `report_file`, writing to `fixes_file`.
   - Pass 1: correctness
   - Pass 2: functional
   - Pass 3: adversarial
   - Pass 4: security (only groups where `security_relevant` is true)

4. **Commit fixes** — if any files changed, stage only fixed files and commit:
   ```bash
   git add <fixed-files>
   git commit -m "fix: apply rechecker fixes [rechecker: skip]"
   ```

5. **Finalize** — use `manifest.session` values:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/finalize-session.py" \
       --uuid "<session.uuid>" --commit "<session.commit>" --start "<session.started>" \
       --report-dir "<session.report_dir>" --snapshot "<session.snapshot_path>" \
       --files-reviewed <files_total> --issues-found <N> --issues-fixed <N> [--commit-made]
   ```

## Output

```
--- Recheck: <UUID> (commit <hash>) ---
Files: <N> reviewed (<M> groups) | Lint: <status> | Security: <skipped/triggered>
Pass 1-3: <N issues fixed / clean>
Commit: <yes (hash) / no fixes needed>
Tokens: <total> (input/output/cache breakdown)
Reports: .rechecker/reports/<UUID>/
---
```

## Error Handling

All stages fail-fast. If triage or any review/fix step fails, the pipeline aborts. No partial commits.

## Examples

```bash
/recheck
```

## Resources

- [review-passes](review-passes.md) — review instructions per pass
  - Shared Rules (append to ALL pass instructions)
  - Large File Instructions (>250KB, opus agent)
  - Pass 1 — Code correctness
  - Pass 2 — Functional correctness
  - Pass 3 — Adversarial review
  - Pass 4 — Security audit (conditional)

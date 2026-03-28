---
name: recheck
description: >
  Use when reviewing and fixing the last committed code changes.
  Trigger with /recheck or after a git commit when the recheck-after-commit rule is active.
---

## Overview

Automated code review and fix pipeline for the latest commit. A triage script handles all mechanical work (file detection, linting, classification). The orchestrator only dispatches review agents from the manifest.

## Prerequisites

Copy this checklist and track your progress:

- [ ] Git repository with at least one commit
- [ ] `${CLAUDE_PLUGIN_ROOT}` set (plugin installed)
- [ ] LLM Externalizer MCP available (for review passes)

## Instructions

1. **Run triage** — the triage script detects files, runs linters, classifies everything, and outputs a JSON manifest:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py"
   ```
   If exit code is 3, stop (recursion guard or no files). If exit code is 0, read the JSON manifest from stdout and proceed. The manifest contains `session`, `files`, `lint`, and `security_pass` fields.

2. **Fix lint errors** — if `manifest.lint.has_errors` is true, spawn `rechecker-plugin:sonnet-code-fixer` with `manifest.lint.files_with_errors` and `manifest.lint.errors_file`. Wait for completion.

3. **Review passes** — dispatch review agents using the pre-split file lists from the manifest. See [review-passes](review-passes.md) for instructions per pass. For `manifest.files.normal`: send to LLM Externalizer `code_task`. For `manifest.files.large`: spawn opus agent per file. After each pass with issues, spawn `rechecker-plugin:sonnet-code-fixer` and wait.
   - Pass 1: correctness
   - Pass 2: functional
   - Pass 3: adversarial
   - Pass 4: security (only if `manifest.security_pass` is true)

4. **Commit fixes** — if any files were changed, stage only fixed files and commit:
   ```bash
   git add <fixed-files>
   git commit -m "fix: apply rechecker fixes [rechecker: skip]"
   ```

5. **Finalize session** — use values from `manifest.session`:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/finalize-session.py" \
       --uuid "<session.uuid>" --commit "<session.commit>" --start "<session.started>" \
       --report-dir "<session.report_dir>" --snapshot "<session.snapshot_path>" \
       --files-reviewed <files.total> --issues-found <N> --issues-fixed <N> [--commit-made]
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

All stages fail-fast. If triage or any review/fix step fails, the pipeline aborts and reports which step failed. No partial commits are made.

## Examples

```bash
# User commits code, then runs recheck
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

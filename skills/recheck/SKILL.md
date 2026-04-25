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
- [ ] `reports/` and `reports_dev/` listed in the project's `.gitignore`
      (add them if missing — reports contain private data)

## Reports Location

Every artifact this plugin writes lives under one canonical root —
`$MAIN_ROOT/reports/rechecker/` — even when the run starts inside a
linked worktree (always the main-repo root, never the worktree's own
`./reports/`). Per-session reviews/fixes/lint captures land in
`$MAIN_ROOT/reports/rechecker/<local-ts+tz>-<uuid>/`; sub-agent output
goes to `reports/rechecker/sonnet-code-fixer/` and
`reports/rechecker/lint-filter/`; the StopFailure hook writes to
`reports/rechecker/stop-failure/`. Runtime state that is not a report
(progress, index, batch lists, commit message) stays under `.rechecker/`.
`/reports/` and `/reports_dev/` must both be in `.gitignore`. Full rule:
`~/.claude/rules/agent-reports-location.md`.

## Instructions

1. **Run triage** — detects files, lints, classifies, splits into groups, outputs manifest:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/triage.py"
   ```
   Exit 3 = skip (recursion guard or no files). Exit 0 = read JSON manifest from stdout. The manifest contains: `session`, `files_total`, `grouped_input_files_paths` (array with GROUP markers for LLM Externalizer), `security_grouped_input_files_paths` (same but only security-relevant groups), `groups[]`, `lint`, `security_pass`. Each group has: `id`, `group_file`, `report_file`, `fixes_file`, `lint_errors_file`, `review_with`, `security_relevant`.

2. **Fix lint errors** — for each group where `lint_errors_file` is not null, spawn `rechecker-plugin:sonnet-code-fixer` with the group's `lint_errors_file` and `group_file`. The agent reads only its own group's files and errors.

3. **Review passes** — for each pass (see [review-passes](review-passes.md)), dispatch reviews using the manifest's pre-built arrays. If `grouped_input_files_paths` is non-empty, pass it as `input_files_paths` to one LLM Externalizer `code_task` call — the `---GROUP:id---` markers produce per-group reports automatically. For `review_with: "opus"` groups: spawn opus agent per group, pass `group_file` in the prompt. After each pass, for groups with issues, spawn `rechecker-plugin:sonnet-code-fixer` with `group_file` + the group's report, writing to `fixes_file`.
   - Pass 1: correctness — use `grouped_input_files_paths`
   - Pass 2: functional — use `grouped_input_files_paths`
   - Pass 3: adversarial — use `grouped_input_files_paths`
   - Pass 4: security — use `security_grouped_input_files_paths` (only security-relevant groups). Skip if empty.

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
Reports: $MAIN_ROOT/reports/rechecker/<SESSION_TS>-<UUID>/
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

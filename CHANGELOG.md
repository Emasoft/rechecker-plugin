# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Features

- Skip files >500KB — too large even for opus[1m]
## [2.1.14] - 2026-03-23

### Features

- Switch file filter from allowlist to blocklist approach

### Miscellaneous Tasks

- Bump version to 2.1.14
## [2.1.13] - 2026-03-23

### Bug Fixes

- Remove duplicate entries in CHANGELOG

### Features

- Filter files to only recheck source code and critical configs

### Miscellaneous Tasks

- Bump version to 2.1.13

### Rechecker

- Automated review fixes
- Automated review fixes
## [2.1.12] - 2026-03-23

### Bug Fixes

- Set huge-file threshold to 100KB (~25K tokens)

### Miscellaneous Tasks

- Bump version to 2.1.12

### Rechecker

- Automated review fixes
## [2.1.11] - 2026-03-23

### Bug Fixes

- Lower huge-file threshold to 80KB (~20K tokens)

### Miscellaneous Tasks

- Bump version to 2.1.11
## [2.1.10] - 2026-03-23

### Bug Fixes

- Use file size (100KB) instead of line count for huge-file threshold

### Miscellaneous Tasks

- Bump version to 2.1.10
## [2.1.9] - 2026-03-23

### Bug Fixes

- Lower huge-file threshold from 5000 to 1500 lines (~19K tokens)

### Miscellaneous Tasks

- Bump version to 2.1.9
## [2.1.8] - 2026-03-23

### Features

- Big-files-auditor agent for files >5000 lines

### Miscellaneous Tasks

- Bump version to 2.1.8

### Rechecker

- Automated review fixes
## [2.1.7] - 2026-03-23

### Bug Fixes

- Remove extraneous f-prefix (ruff F541)

### Features

- Make rechecker notification unmissable + rewrite README for v2.1.x

### Miscellaneous Tasks

- Bump version to 2.1.7
## [2.1.6] - 2026-03-23

### Bug Fixes

- Update recheck skill to use merge-worktrees.sh and current architecture

### Miscellaneous Tasks

- Bump version to 2.1.6
## [2.1.5] - 2026-03-23

### Features

- Merge-worktrees.sh fully automated with auto-stash and -X ours

### Miscellaneous Tasks

- Bump version to 2.1.5

### Rechecker

- Automated review fixes
## [2.1.4] - 2026-03-22

### Bug Fixes

- Ignore submodule dirtiness in merge-worktrees.sh clean check

### Miscellaneous Tasks

- Bump version to 2.1.4

### Rechecker

- Automated review fixes
- Automated review fixes
## [2.1.3] - 2026-03-22

### Bug Fixes

- Add mypy type checking to publish script lint stage

### Miscellaneous Tasks

- Bump version to 2.1.3
## [2.1.2] - 2026-03-22

### Bug Fixes

- Resolve mypy type errors in pipeline.py and resume-check.py

### Miscellaneous Tasks

- Bump version to 2.1.2
## [2.1.1] - 2026-03-22

### Bug Fixes

- Ensure TLDR artifacts are gitignored in worktree projects
- Also enforce TLDR gitignore inside worktree (belt-and-suspenders)
- Make rechecker output visible to Claude and include report summaries
- Update .gitignore and fix merge-worktrees.sh bugs
- Remove extraneous f-prefix in resume-check.py (ruff F541)

### Features

- Add resume support for interrupted rechecker runs
- Bundle merge-worktrees.sh and copy to .rechecker/ at runtime
- Make merge-worktrees.sh fully standalone and self-cleaning

### Miscellaneous Tasks

- Bump version to 2.1.0
- Sync version to 2.1.0 in pyproject.toml and README badge
- Bump version to 2.1.1
## [2.0.51] - 2026-03-22

### Bug Fixes

- Strict FID validation — exactly 5 digits required after FID prefix
- Strict tag validation — reject all invalid bracket combinations
- Prevent recursive triggering + reduce token consumption
- Adapt externalizer instructions for its actual capabilities
- Use function names + code quotes instead of line numbers in reviews
- Free-form markdown reviews instead of rigid JSON format
- Bugs found by LLM Externalizer code review
- Remove extraneous f-prefix (ruff F541)

### Features

- Add pipeline.py for file grouping, report merging, and issue counting
- Use LLM Externalizer for code reviews instead of opus agents

### Miscellaneous Tasks

- Bump version to 2.0.51
## [2.0.50] - 2026-03-22

### Bug Fixes

- Timestamps reflect exact moment each file is written

### Miscellaneous Tasks

- Bump version to 2.0.50
## [2.0.49] - 2026-03-22

### Features

- Consistent rck- naming convention across all files

### Miscellaneous Tasks

- Bump version to 2.0.49
## [2.0.48] - 2026-03-22

### Bug Fixes

- Hook writes RECHECKER_MERGE_PENDING.md for Claude + systemMessage for user

### Miscellaneous Tasks

- Bump version to 2.0.48
## [2.0.47] - 2026-03-22

### Bug Fixes

- Don't auto-merge worktree — let main Claude merge when ready
- Remove f-prefix from string without placeholders (ruff F541)

### Miscellaneous Tasks

- Bump version to 2.0.47
## [2.0.46] - 2026-03-22

### Bug Fixes

- Collect report from worktree dir after headless orchestrator exit

### Miscellaneous Tasks

- Bump version to 2.0.46
## [2.0.45] - 2026-03-21

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.45
## [2.0.44] - 2026-03-21

### Bug Fixes

- Use unique worktree names + flush log before orchestrator launch

### Miscellaneous Tasks

- Bump version to 2.0.43
- Bump version to 2.0.44

### Testing

- Verify PostToolUse fires for git commit
- Verify PostToolUse fires for git commit
## [2.0.43] - 2026-03-21

### Bug Fixes

- Revert to simple git-commit detection + log before all gates

### Miscellaneous Tasks

- Bump version to 2.0.43
## [2.0.42] - 2026-03-21

### Bug Fixes

- Detect commits by HEAD tracking, not command text parsing
- Remove extraneous f-string prefixes from log calls

### Miscellaneous Tasks

- Bump version to 2.0.42
## [2.0.41] - 2026-03-21

### Bug Fixes

- Add diagnostic logging to hook script + try tool_input.cwd

### Miscellaneous Tasks

- Bump version to 2.0.41
## [2.0.40] - 2026-03-21

### Bug Fixes

- Skill must not delete worktrees — Claude Code handles cleanup automatically

### Documentation

- Rewrite README with clear installation, usage, and architecture sections

### Miscellaneous Tasks

- Bump version to 2.0.40

### Rechecker

- Automated review fixes
- Automated review fixes
## [2.0.39] - 2026-03-21

### Bug Fixes

- Use agent name (not file path) for --agent flag, add -p prompt

### Miscellaneous Tasks

- Bump version to 2.0.39
## [2.0.38] - 2026-03-21

### Bug Fixes

- Robust git detection for submodules, subdirs, and missing repos
- Skill must cd to git root before launching claude --worktree
- Use inline $(git rev-parse --show-toplevel) instead of placeholder variable in skill

### Miscellaneous Tasks

- Pre-edit snapshot before fixing git detection and worktree isolation
- Bump version to 2.0.38
## [2.0.37] - 2026-03-21

### Bug Fixes

- Skill uses default agent, spawns orchestrator explicitly (worktree isolation stays with orchestrator, report cleanup in main context)

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.37
## [2.0.36] - 2026-03-21

### Bug Fixes

- Skill moves report to reports_dev/ after worktree merge

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.36
## [2.0.35] - 2026-03-21

### Bug Fixes

- Move report to reports_dev/ immediately after worktree merge (not next run)

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.35
## [2.0.34] - 2026-03-21

### Bug Fixes

- Auto-move old rechecker reports to reports_dev/ on next hook run

### Miscellaneous Tasks

- Bump version to 2.0.34
## [2.0.33] - 2026-03-21

### Bug Fixes

- Save timestamped report to worktree root (committed + merged to main)

### Miscellaneous Tasks

- Bump version to 2.0.33
## [2.0.32] - 2026-03-21

### Bug Fixes

- Final audit — loop refs, crash detection, ephemeral report docs

### Miscellaneous Tasks

- Bump version to 2.0.32
## [2.0.31] - 2026-03-21

### Bug Fixes

- Strict file-based protocol — no inline data in prompts

### Miscellaneous Tasks

- Bump version to 2.0.31
## [2.0.30] - 2026-03-21

### Bug Fixes

- Data flow between agents — inline prompt, not file exchange

### Miscellaneous Tasks

- Bump version to 2.0.30
## [2.0.29] - 2026-03-21

### Bug Fixes

- Audit — filename consistency, missing mkdir, dead code, report path

### Miscellaneous Tasks

- Bump version to 2.0.29
## [2.0.28] - 2026-03-21

### Documentation

- Complete README rewrite + audit fixes for 4-agent architecture

### Miscellaneous Tasks

- Bump version to 2.0.28
## [2.0.27] - 2026-03-21

### Features

- Complete 4-agent architecture with skill frontmatter

### Miscellaneous Tasks

- Bump version to 2.0.27
## [2.0.26] - 2026-03-21

### Bug Fixes

- Add & to skill command for async execution

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.26
## [2.0.25] - 2026-03-21

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.25

### Performance

- Launch claude --worktree async (Popen, non-blocking)
## [2.0.24] - 2026-03-21

### Documentation

- Update SKILL.md for 4-agent architecture

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.24
## [2.0.23] - 2026-03-21

### Features

- 4-agent architecture with single-commit worktree

### Miscellaneous Tasks

- Bump version to 2.0.23
## [2.0.22] - 2026-03-21

### Miscellaneous Tasks

- Bump version to 2.0.22

### Refactor

- Lint once at start+end, remove dead orchestration scripts
## [2.0.21] - 2026-03-21

### Features

- Iterative check→fix loop inside agents, shared named worktree

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.21
## [2.0.20] - 2026-03-21

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.20

### Performance

- Two-swarm pattern — opus finds bugs, sonnet fixes them
## [2.0.19] - 2026-03-21

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.19

### Performance

- Switch agents from opus[1m] to sonnet (5-10x faster reviews)
## [2.0.18] - 2026-03-21

### Bug Fixes

- Use hooks.json async:true instead of Popen fork for non-blocking

### Features

- Multi-repo support — detect all git repos from compound commands
- Non-blocking hook + multi-repo recheck with recent commit scan
- Simplify hook to direct claude --worktree launch

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.18

### Refactor

- Simplify rechecker to just detect + launch claude --worktree
## [2.0.17] - 2026-03-21

### Bug Fixes

- Hook silently skips commits when cwd is not the git repo root ([#1](https://github.com//rechecker-plugin/issues/1))

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.17
## [2.0.16] - 2026-03-20

### Bug Fixes

- Remove stale scan.sh refs, rename skip_scan, clean test artifact
- CPV validation — 4 of 7 MINOR issues fixed
- CPV validation — all MAJOR/MINOR fixed (2 remaining are false positives)

### Features

- Add find_duplicates and clamp utility functions
- Replace Docker scan with direct linters + parallel subagent review

### Miscellaneous Tasks

- Bump version to 2.0.16
## [2.0.15] - 2026-03-20

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.15
## [2.0.14] - 2026-03-20

### Bug Fixes

- Remove duplicate hooks reference from plugin.json (auto-loaded by convention)

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.14
## [2.0.13] - 2026-03-20

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.13

### Rechecker

- Pass 1 fixes
## [2.0.12] - 2026-03-20

### Bug Fixes

- Scan.sh fails on first run due to --skip-pull with no cached images
- Super-Linter --platform linux/amd64 for Apple Silicon (no ARM64 image)
- Scan.sh Super-Linter result JSON was malformed (grep -c bug)

### Features

- Add example utility with safe_divide and parse_config

### Miscellaneous Tasks

- Update uv.lock, remove stray scan report
- Bump version to 2.0.12
## [2.0.11] - 2026-03-20

### Bug Fixes

- Publish.py auto-commits uv.lock if only dirty file

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.11
## [2.0.10] - 2026-03-20

### Miscellaneous Tasks

- Add CLAUDE.md to gitignore (user-local, never committed)
- Update uv.lock
- Bump version to 2.0.10
## [2.0.9] - 2026-03-20

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.9
## [2.0.8] - 2026-03-20

### Documentation

- Explain two entry points and _shared.py in README Scripts section

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.8
## [2.0.7] - 2026-03-20

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.7

### Refactor

- Extract shared code into _shared.py (fix jscpd 6.15% > 5%)
## [2.0.6] - 2026-03-20

### Bug Fixes

- Release workflow install dev deps (ruff/mypy need --extra dev)

### Features

- Publish.py auto-updates README version badge

### Miscellaneous Tasks

- Update uv.lock
- Bump version to 2.0.6
## [2.0.5] - 2026-03-20

### Bug Fixes

- CI workflows use uv sync --extra dev (installs ruff, pytest, pyyaml)
- Version badge, stale .sh reference, dead code, formatting
- Audit fixes — double escaping, phase 2 diff, exit codes, docs
- Stderr log path, diff_stat consistency, README completeness
- Add claude CLI check to recheck.py, fix README var names
- Path traversal defense, cross-platform CLI check, gh flag conflict
- Quote paths in prompt for spaces, use original commit msg in Phase 2
- Publish.py skip tests when tests/ has no test_*.py files

### Features

- Standardize plugin for CPV canonical pipeline
- Add functionality-reviewer agent (Phase 2 of two-phase pipeline)

### Fix

- Scan only changed files, not entire codebase

### Miscellaneous Tasks

- Configure marketplace notification for Emasoft/emasoft-plugins
- Bump version to 2.0.5

### Refactor

- Use 'claude --worktree' instead of manual git worktree management

### Bump

- Version 2.0.0 → 2.0.1
- Version 2.0.1 → 2.0.2
- Version 2.0.2 → 2.0.3
- Version 2.0.3 → 2.0.4
---
*Generated by [git-cliff](https://git-cliff.org)*

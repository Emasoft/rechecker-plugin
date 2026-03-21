# rechecker-plugin

<!--BADGES-START-->
[![CI](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-2.0.19-blue)](https://github.com/Emasoft/rechecker-plugin)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Validation](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml)
<!--BADGES-END-->

A Claude Code plugin that automatically reviews and fixes code changes after every git commit. It spawns a separate Claude instance in an isolated git worktree, runs automated linters (ruff, mypy, shellcheck), performs a parallel code review via subagents, fixes all issues found, and loops until the code is clean — or reports what remains.

**v2.0.0**: All scripts rewritten in Python 3 (except scan.sh). Agent model upgraded to Opus 4.6 (1M context). Added `/recheck` slash command for on-demand reviews. Added LLM Externalizer MCP integration for offloading read-only analysis. Full `mypy --strict` type annotations.

## Why

Claude Code often introduces bugs, incomplete implementations, or subtle issues when making code changes. Without this plugin, you have to manually ask Claude to "check your changes for errors, then fix them" — sometimes repeating this **8+ times** before issues reach zero. This plugin automates that entire review-fix cycle.

## How It Works

```
Claude commits code
        |
        v
PostToolUse hook detects "git commit" (not --amend)
        |
        v
Acquires lock (prevents concurrent reviews)
        |
        v
=== PHASE 1: CODE REVIEW (code-reviewer agent) ===
        |
+---> Creates worktree via 'claude --worktree'
|         |
|         v
|     Reset worktree to commit state
|     Run linters (ruff, mypy, shellcheck — no Docker)
|     View git diff, review files in parallel (subagents)
|     Fix bugs
|     Commit fixes, write report with ISSUES_FOUND: N
|         |
|         v
|     Merge fixes, destroy worktree
|     ISSUES_FOUND > 0? YES --> loop (up to 30 passes)
+--- loop
        |
        NO (code is clean)
        |
        v
=== PHASE 2: FUNCTIONALITY REVIEW (functionality-reviewer agent) ===
        |
+---> Creates worktree via 'claude --worktree'
|         |
|         v
|     Reset worktree to commit state
|     View git diff, determine intent from names/docs/tests
|     Verify code does what it's supposed to do
|     Fix discrepancies, write report with ISSUES_FOUND: N
|         |
|         v
|     Merge fixes, destroy worktree
|     ISSUES_FOUND > 0? YES --> loop (up to 30 passes)
+--- loop
        |
        NO (code works correctly)
        |
        v
Inject summary into main Claude's context
(tells Claude to READ the summary reports)
```

## Features

| Feature | Description |
|---------|-------------|
| **Automatic trigger** | PostToolUse hook fires after every `git commit` (skips `--amend`) |
| **Worktree isolation** | Review agent works in a separate `claude --worktree`, never touching the main branch until merge |
| **Direct linting** | Runs ruff, mypy, shellcheck directly on changed files (no Docker needed) |
| **Parallel review** | Spawns one subagent per changed file for parallel code review |
| **Autofix** | Review agent fixes issues in place |
| **Iterative loop** | Repeats review-fix cycle until `ISSUES_FOUND: 0` or max 30 passes |
| **Transient error resilience** | Retries `claude --worktree` on rate limits, 429, 503, 502, timeouts (3 retries with backoff) |
| **Agent failure detection** | If reviewer finds issues but fails to commit fixes twice in a row, breaks and reports agent bug |
| **Merge conflict handling** | Aborts merge on conflict, reports it, stops the loop |
| **Detailed reports** | Per-pass reports + summary saved to `reports_dev/` |
| **Execution checklist** | 9-item mandatory checklist the agent must complete before exiting |
| **StopFailure logging** | Logs API errors (rate limits, server errors) to `reports_dev/rechecker_api_errors.log` |
| **Lock file** | PID-based lock prevents concurrent review cycles |
| **On-demand review** | `/recheck` slash command triggers the same review loop manually on any commit |
| **Cross-platform** | Python 3.12+ on macOS, Linux, WSL (scan.sh requires Bash) |

## Plugin Structure

```
rechecker-plugin/
+-- .claude-plugin/
|   +-- plugin.json              # Plugin manifest
+-- hooks/
|   +-- hooks.json               # PostToolUse on Bash + StopFailure logging
+-- agents/
|   +-- code-reviewer.md         # Phase 1: code correctness, bugs, security
|   +-- functionality-reviewer.md # Phase 2: does the code do what it's supposed to
+-- skills/
|   +-- recheck/
|       +-- SKILL.md             # /recheck slash command: on-demand review trigger
+-- scripts/
|   +-- rechecker.py             # Hook entry point: detects git commit, outputs hook JSON
|   +-- recheck.py               # Skill entry point: /recheck slash command, plain stdout
|   +-- _shared.py               # Shared logic: lock management, two-phase orchestration
|   +-- review-loop.py           # Core loop: worktree, scan, review, merge, retry
|   +-- changed-files.py         # Generates list of changed files from git commit
|   +-- scan.sh                  # Optional: Super-Linter + Semgrep + TruffleHog via Docker
|   +-- log-stop-failure.py      # StopFailure hook: logs API errors
|   +-- publish.py               # Dev tool: bump version, tag, push, release
+-- .gitignore
+-- README.md
```

## Scripts

The review pipeline has **two entry points** that share the same underlying logic via `_shared.py`:

| Entry Point | Trigger | How it starts | I/O format |
|-------------|---------|---------------|------------|
| `rechecker.py` | **Automatic** — PostToolUse hook fires after every `git commit` | Claude Code calls it via `hooks.json`, passes JSON on stdin | Reads hook JSON, outputs `additionalContext` JSON |
| `recheck.py` | **Manual** — user types `/recheck` or `/recheck abc1234` | Claude runs it via the Bash tool with an optional SHA argument | Reads argv, prints plain text results |

Both entry points validate the environment (git repo, `claude` CLI on PATH), acquire a PID-based lock, then delegate to `_shared.run_two_phase_review()` which orchestrates Phase 1 and Phase 2. The only difference is how they receive input and format output.

| Script | Purpose | Input | Output |
|--------|---------|-------|--------|
| `rechecker.py` | Hook entry point. Detects `git commit` in Bash tool calls, acquires lock, runs two-phase review | JSON on stdin | JSON on stdout (`additionalContext`) |
| `recheck.py` | Skill entry point for `/recheck`. Same two-phase pipeline, different I/O | `[commit_sha]` (optional, defaults to HEAD) | Phase results on stdout |
| `_shared.py` | Shared logic used by both entry points: lock management, claude CLI check, two-phase review orchestration | Imported as module | N/A |
| `review-loop.py` | Core review loop. Creates worktrees, runs scan + review agent, merges fixes, iterates until clean. Exit 0 = clean, 1 = issues remain | 6 positional args + optional: agent file, `--func-review`, `--original-commit <sha>` | Summary text on stdout |
| `changed-files.py` | Generates list of files changed in a commit. Handles first commits, merge commits, excludes deleted files | `<commit_sha> [output_file]` | File paths (one per line) to stdout or file |
| `scan.sh` | Optional: Runs Super-Linter, Semgrep, TruffleHog via Docker. Not used by default (linters run directly instead) | CLI flags + project dir | JSON report path on stdout |
| `log-stop-failure.py` | Logs StopFailure events (rate limits, server errors) for debugging | JSON on stdin | Appends to `rechecker_api_errors.log` |
| `publish.py` | Dev tool: test, lint, validate, bump version (including README badge), tag, push, create GitHub release | `--patch\|--minor\|--major [--dry-run]` | Console output |

## Agents

### Phase 1: code-reviewer

Checks code correctness, bugs, and security. Runs linters directly (ruff, mypy, shellcheck), then reviews files in parallel via subagents.

| Section | Contents |
|---------|----------|
| **Frontmatter** | model: opus[1m], all tools allowed |
| **Review Checklist** | Correctness, Security, Error Handling, API Contracts, Code Correctness |
| **What NOT to Check** | Style, performance (unless algorithmic), features, refactoring, docs |
| **Execution Checklist** | 9 mandatory items the agent must complete before exiting |

### Phase 2: functionality-reviewer

Verifies code actually does what it is supposed to do. Runs only after Phase 1 completes with 0 issues. No scan.sh step.

| Section | Contents |
|---------|----------|
| **Frontmatter** | model: opus[1m], all tools allowed |
| **Review Checklist** | Intent Verification, Behavioral Correctness, Requirements Coverage, I/O Contract, Integration |
| **What NOT to Check** | Syntax, type errors, security (already handled by Phase 1) |
| **Execution Checklist** | 9 mandatory items the agent must complete before exiting |

## Review Checklist (What the Agent Checks)

| Category | Severity | What It Checks |
|----------|----------|----------------|
| **Correctness** | CRITICAL | Logic errors, null handling, type mismatches, edge cases, race conditions, resource leaks |
| **Security** | CRITICAL | Injection (SQL/XSS/command), path traversal, hardcoded secrets, insecure defaults |
| **Error Handling** | HIGH | Swallowed exceptions, missing propagation, inconsistent handling, missing validation |
| **API Contracts** | HIGH | Breaking changes, missing returns, incorrect API usage |
| **Code Correctness** | MEDIUM | Dead code, missing imports, broken references, copy-paste errors |

## Reports

Reports are saved to `<project>/reports_dev/` with these files:

| File | Contents |
|------|----------|
| `rechecker_<ts>_pass<N>.md` | Per-pass review report (issues found, fixed, linter results) |
| `rechecker_<ts>_summary.md` | Final summary (status, total issues, pass details) |
| `rechecker_api_errors.log` | API error log (rate limits, server errors) |

## Loop Termination Conditions

| Condition | Result |
|-----------|--------|
| `ISSUES_FOUND: 0` in a valid report | Exit clean |
| Max 30 passes reached | Exit with warning |
| Merge conflict | Exit, report conflict |
| Reviewer fails to commit fixes 2x in a row | Exit, report agent bug |
| Dirty working directory before merge | Exit, report error |
| Worktree creation fails | Exit, report error |

## Error Resilience

| Error Type | Handling |
|------------|----------|
| Rate limit (429) | Retry 3x with 30/60/90s backoff |
| Server error (502/503/504) | Retry 3x with backoff |
| Timeout / ECONNRESET | Retry 3x with backoff |
| Auth failure / invalid request | No retry (non-transient) |
| Linter not installed | Skipped, noted in Checklist Failures section of report |
| No changed files | Clean exit (nothing to review) |
| First commit (no parent) | Handled via `git show` fallback |

## Components

| Type | Name | Description |
|------|------|-------------|
| Hook | `PostToolUse` → `rechecker.py` | Auto-triggers two-phase review after every `git commit` |
| Hook | `StopFailure` → `log-stop-failure.py` | Logs API errors (rate limits, server errors) |
| Agent | `code-reviewer` | Phase 1: Opus 4.6 (1M) — code correctness, bugs, security |
| Agent | `functionality-reviewer` | Phase 2: Opus 4.6 (1M) — verifies code does what it's supposed to |
| Skill | `/recheck` | On-demand two-phase review trigger for any commit |

## Installation

### From Marketplace

```bash
claude plugin marketplace add Emasoft/emasoft-plugins
claude plugin install rechecker-plugin@emasoft-plugins
```

### From GitHub

```bash
claude plugin install --source github Emasoft/rechecker-plugin
```

### Manual (Development)

```bash
# Symlink (recommended for development)
ln -s /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin

# Or copy
cp -r /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin
```

Restart Claude Code, then type `/hooks` to confirm the PostToolUse hook appears.

## Uninstall

```bash
claude plugin uninstall rechecker-plugin
```

## Update

```bash
claude plugin update rechecker-plugin@emasoft-plugins
```

## Configuration

The plugin works out of the box with no configuration. Key defaults:

| Setting | Default | Where |
|---------|---------|-------|
| Max passes | 30 | `review-loop.py` (`max_passes`) |
| Hook timeout | 24 hours | `hooks/hooks.json` |
| Retry count | 3 | `review-loop.py` (`max_retries`) |
| Retry delay | 30s base | `review-loop.py` (`retry_delay`) |
| Agent model | opus[1m] | `agents/code-reviewer.md` frontmatter |
| Permission mode | bypass all | `review-loop.py` (`--dangerously-skip-permissions`) |

### Gitignore

Add these to your project's `.gitignore`:

```
.rechecker/
reports_dev/
```

## Requirements

| Requirement | Why |
|-------------|-----|
| `claude` CLI on PATH | Runs the review agent in headless mode |
| `python3` (3.12+) on PATH | All scripts are Python 3.12+ (except scan.sh) |
| `git` repository | Worktrees, diffs, commits |
| Docker (optional) | Only needed if using scan.sh manually (not used by default) |
| Max subscription | `claude --worktree` uses your Max subscription auth |

## Cross-Platform Compatibility

| Platform | Status | Notes |
|----------|--------|-------|
| macOS | Supported | Python 3.12+ required |
| Linux | Supported | Python 3.12+ required |
| WSL | Supported | Docker may need extra setup |
| Windows | Untested | Python 3.12+ required, scan.sh needs Git Bash |

## Safety

| Concern | Mitigation |
|---------|------------|
| Git state corruption | Merge conflicts abort cleanly; worktrees are isolated |
| Accidental file deletion | `cleanup_worktree` validates path contains `/.claude/worktrees/` before forced removal |
| Concurrent reviews | PID-based lock file with stale lock detection |
| Infinite loops | Max 30 passes + no-fix detection (breaks after 2 consecutive failures) |

## Troubleshooting

### Hook path not found
If you get "can't open file" errors from hooks, reinstall the plugin or check that `${CLAUDE_PLUGIN_ROOT}` resolves correctly in your Claude Code session.

### Old version after update
Claude Code may cache the old version. Restart Claude Code to pick up changes.

### Restart required after update
After updating the plugin, restart Claude Code to reload all hooks and agents.

## License

MIT — see [LICENSE](LICENSE)

## Author

**Emasoft** — [GitHub](https://github.com/Emasoft)

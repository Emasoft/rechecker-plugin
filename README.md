# rechecker-plugin

A Claude Code plugin that automatically reviews and fixes code changes after every git commit. It spawns a separate Claude instance in an isolated git worktree, runs automated linters and security scanners, performs a thorough manual code review, fixes all issues found, and loops until the code is clean — or reports what remains.

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
+---> Creates worktree via 'claude --worktree'
|         |
|         v
|     STEP 1: Reset worktree to commit state
|     STEP 2: Run scan.sh (Super-Linter + Semgrep + TruffleHog)
|              with --autofix --target-list (changed files only)
|     STEP 3: View git diff of the commit
|     STEP 4: Manual code review (agent reads full files)
|     STEP 5: Fix all issues found
|     STEP 6: Commit fixes (scan autofix + manual fixes)
|     STEP 7: Write detailed report with ISSUES_FOUND: N
|         |
|         v
|     Copy report to reports_dev/
|     Merge worktree branch into main
|     Destroy worktree
|         |
|         v
|     ISSUES_FOUND > 0?
|       YES --> loop back (up to 30 passes)
|       NO  --> done, code is clean
|
+--- loop
        |
        v
Inject summary into main Claude's context
(tells Claude to READ the summary report)
```

## Features

| Feature | Description |
|---------|-------------|
| **Automatic trigger** | PostToolUse hook fires after every `git commit` (skips `--amend`) |
| **Worktree isolation** | Review agent works in a separate `claude --worktree`, never touching the main branch until merge |
| **Automated scanning** | Runs `scan.sh` first: Super-Linter (40+ linters), Semgrep (OWASP security), TruffleHog (secrets) |
| **Targeted scanning** | Only scans files changed in the commit via `--target-list` (not the whole codebase) |
| **Autofix** | Both scan.sh and the review agent fix issues in place |
| **Iterative loop** | Repeats review-fix cycle until `ISSUES_FOUND: 0` or max 30 passes |
| **Transient error resilience** | Retries `claude --worktree` on rate limits, 429, 503, 502, timeouts (3 retries with backoff) |
| **Agent failure detection** | If reviewer finds issues but fails to commit fixes twice in a row, breaks and reports agent bug |
| **Merge conflict handling** | Aborts merge on conflict, reports it, stops the loop |
| **Detailed reports** | Per-pass reports + summary saved to `reports_dev/` |
| **Execution checklist** | 10-item mandatory checklist the agent must complete before exiting |
| **StopFailure logging** | Logs API errors (rate limits, server errors) to `reports_dev/rechecker_api_errors.log` |
| **Lock file** | PID-based lock prevents concurrent review cycles |
| **Cross-platform** | Bash 3.2+ (macOS), Bash 4/5 (Linux), WSL compatible |

## Plugin Structure

```
rechecker-plugin/
+-- .claude-plugin/
|   +-- plugin.json              # Plugin manifest
+-- hooks/
|   +-- hooks.json               # PostToolUse on Bash + StopFailure logging
+-- agents/
|   +-- code-reviewer.md         # Agent definition with review checklist + execution checklist
+-- scripts/
|   +-- rechecker.sh             # Entry point: commit detection, locking, JSON I/O
|   +-- review-loop.sh           # Core loop: worktree, scan, review, merge, retry
|   +-- changed-files.sh         # Generates list of changed files from git commit
|   +-- scan.sh                  # Runs Super-Linter + Semgrep + TruffleHog via Docker
|   +-- log-stop-failure.sh      # StopFailure hook: logs API errors
+-- .gitignore
+-- README.md
```

## Scripts

| Script | Purpose | Input | Output |
|--------|---------|-------|--------|
| `rechecker.sh` | Hook entry point. Reads PostToolUse JSON from stdin, detects `git commit`, acquires lock, invokes `review-loop.sh` | JSON on stdin | JSON on stdout (`additionalContext`) |
| `review-loop.sh` | Core review loop. Creates worktrees, runs scan + review agent, merges fixes, iterates until clean | 6 positional args (project dir, commit SHA, branch, reports dir, timestamp, plugin root) | Summary text on stdout |
| `changed-files.sh` | Generates list of files changed in a commit. Handles first commits, merge commits, excludes deleted files | `<commit_sha> [output_file]` | File paths (one per line) to stdout or file |
| `scan.sh` | Runs Super-Linter, Semgrep, TruffleHog via Docker. Auto-installs Docker if needed. Supports `--target-list` for targeted scanning | CLI flags + project dir | JSON report path on stdout |
| `log-stop-failure.sh` | Logs StopFailure events (rate limits, server errors) for debugging | JSON on stdin | Appends to `rechecker_api_errors.log` |

## Agent: code-reviewer

The `agents/code-reviewer.md` defines the review agent with:

| Section | Contents |
|---------|----------|
| **Frontmatter** | model: sonnet, allowedTools: Read, Edit, Write, Bash, Glob, Grep |
| **Workflow** | 7-step process: scan, diff, review, fix, commit, report |
| **Review Checklist** | Correctness, Security, Error Handling, API Contracts, Code Correctness |
| **What NOT to Check** | Style, performance (unless algorithmic), features, refactoring, docs |
| **Report Format** | Markdown with Issues Found, Scan Results, Files Reviewed, ISSUES_FOUND/FIXED counts |
| **Rules for Fixing** | Minimal fixes, preserve intent, no new features, uncertain = report only |
| **Execution Checklist** | 10 mandatory items the agent must complete before exiting |

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
| `rechecker_<ts>_pass<N>.md` | Per-pass review report (issues found, fixed, scan results) |
| `rechecker_<ts>_summary.md` | Final summary (status, total issues, pass details) |
| `scan-report-*.json` | Raw scan.sh JSON output (Super-Linter + Semgrep + TruffleHog) |
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
| Docker not available | Scan skipped, manual review continues |
| Scan fails | Documented in Checklist Failures section of report |
| No changed files | Clean exit (nothing to review) |
| First commit (no parent) | Handled via `git show` fallback |

## Installation

```bash
# Symlink (recommended for development)
ln -s /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin

# Or copy
cp -r /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin
```

Restart Claude Code, then type `/hooks` to confirm the PostToolUse hook appears.

## Configuration

The plugin works out of the box with no configuration. Key defaults:

| Setting | Default | Where |
|---------|---------|-------|
| Max passes | 30 | `review-loop.sh` (`MAX_PASSES`) |
| Scan timeout | 3 hours | `review-loop.sh` (prompt `--scan-timeout`) |
| Hook timeout | 24 hours | `hooks/hooks.json` |
| Retry count | 3 | `review-loop.sh` (`MAX_RETRIES`) |
| Retry delay | 30s base | `review-loop.sh` (`RETRY_DELAY`) |
| Agent model | sonnet | `agents/code-reviewer.md` frontmatter |
| Permission mode | bypass all | `review-loop.sh` (`--dangerously-skip-permissions`) |

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
| `python3` on PATH | JSON parsing in hook scripts |
| `git` repository | Worktrees, diffs, commits |
| Docker (optional) | Required for scan.sh (Super-Linter, Semgrep, TruffleHog) |
| Max subscription | `claude --worktree` uses your Max subscription auth |

## Cross-Platform Compatibility

| Platform | Status | Notes |
|----------|--------|-------|
| macOS (Bash 3.2) | Supported | `set -o pipefail` wrapped in fallback |
| macOS (Bash 5 via Homebrew) | Supported | Full feature set |
| Linux (Bash 4+) | Supported | Full feature set |
| WSL | Supported | Docker may need extra setup |
| Windows (Git Bash) | Untested | Should work if Docker available |

## Safety

| Concern | Mitigation |
|---------|------------|
| Git state corruption | Merge conflicts abort cleanly; worktrees are isolated |
| Accidental file deletion | `cleanup_worktree` validates path contains `/.claude/worktrees/` before forced removal |
| Scan report pollution | Scan output goes to `.rechecker_scan_output/` subdirectory, cleaned up after reading |
| Concurrent reviews | PID-based lock file with stale lock detection |
| Infinite loops | Max 30 passes + no-fix detection (breaks after 2 consecutive failures) |
| Secret exposure | TruffleHog detects secrets; scan runs in Docker sandbox |

## License

MIT

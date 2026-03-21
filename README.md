# rechecker-plugin

<!--BADGES-START-->
[![CI](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-2.0.32-blue)](https://github.com/Emasoft/rechecker-plugin)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Validation](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml)
<!--BADGES-END-->

A Claude Code plugin that automatically reviews and fixes code changes after every git commit. It launches an orchestrator agent in a git worktree that coordinates parallel swarms of opus reviewers and sonnet fixers, iterating until the code is clean.

## Why

Claude Code often introduces bugs, incomplete implementations, or subtle issues when making code changes. Without this plugin, you have to manually ask Claude to "check your changes for errors, then fix them" — sometimes repeating this **8+ times** before issues reach zero. This plugin automates that entire review-fix cycle.

## How It Works

```
Claude commits code
        |
        v
PostToolUse hook detects "git commit" (not --amend) [async: true]
        |
        v
rechecker.py finds all git repos in the command
        |
        v
For each repo: launches orchestrator in a named worktree
        |
        v
=== RECHECKER ORCHESTRATOR (1 worktree, 1 commit) ===
        |
        v
LOOP 1: Lint → sonnet-code-fixer swarm → repeat until 0 lint errors
        |
        v
LOOP 2: opus-code-reviewer swarm finds bugs
        → sonnet-code-fixer swarm fixes → repeat until 0 bugs
        |
        v
LOOP 3: opus-functionality-reviewer swarm checks intent
        → sonnet-code-fixer swarm fixes → repeat until 0 intent issues
        |
        v
LOOP 4: Final lint → sonnet-code-fixer swarm → repeat until 0
        |
        v
Merge reports → ONE commit → exit
        |
        v
Claude Code merges worktree back to main
```

## Features

| Feature | Description |
|---------|-------------|
| **Automatic trigger** | PostToolUse hook fires after every `git commit` (skips `--amend`), runs async |
| **4-agent swarm architecture** | Opus orchestrator + opus reviewers + sonnet fixers in parallel swarms |
| **Worktree isolation** | All work happens in a named `claude --worktree`, merged once at the end |
| **Single commit** | All 4 loops complete before any commit — no intermediate commits |
| **Direct linting** | Runs ruff, mypy, shellcheck directly (no Docker needed) |
| **Parallel review** | One subagent per file, all spawned in parallel |
| **Iterative loops** | Each loop repeats check→fix until 0 issues (max 30 passes) |
| **Multi-repo support** | Detects all git repos in compound commands (`cd /repo1 && git commit && cd /repo2 && git commit`) |
| **On-demand review** | `/recheck` skill triggers the same pipeline manually |
| **StopFailure logging** | Logs API errors to `reports_dev/rechecker_api_errors.log` |
| **Cross-platform** | Python 3.12+ on macOS, Linux, WSL |

## Plugin Structure

```
rechecker-plugin/
+-- .claude-plugin/
|   +-- plugin.json                     # Plugin manifest
+-- hooks/
|   +-- hooks.json                      # PostToolUse (async) + StopFailure
+-- agents/
|   +-- rechecker-orchestrator.md         # Opus orchestrator: runs all 4 loops
|   +-- opus-code-reviewer.md           # Opus swarm worker: finds correctness bugs
|   +-- opus-functionality-reviewer.md  # Opus swarm worker: checks intent vs reality
|   +-- sonnet-code-fixer.md            # Sonnet swarm worker: applies fixes
+-- skills/
|   +-- recheck/
|       +-- SKILL.md                    # /recheck: on-demand review (context:fork)
+-- scripts/
|   +-- rechecker.py                    # Hook entry point: detect commit, launch orchestrator
|   +-- log-stop-failure.py             # StopFailure hook: log API errors
|   +-- scan.sh                         # Optional: Super-Linter + Semgrep + TruffleHog
|   +-- publish.py                      # Dev tool: bump version, tag, push, release
+-- .gitignore
+-- README.md
```

## Agents

| Agent | Model | Role | Output |
|-------|-------|------|--------|
| `rechecker-orchestrator` | opus[1m] | Coordinates all 4 loops, spawns swarms, merges reports, makes 1 commit | `rechecker-report.md` |
| `opus-code-reviewer` | opus[1m] | Reviews one file for correctness bugs (13 categories). Does NOT fix. | JSON findings array |
| `opus-functionality-reviewer` | opus[1m] | Verifies one file does what it claims (9 categories). Does NOT fix. | JSON findings array |
| `sonnet-code-fixer` | sonnet | Fixes bugs from a report file. Root-cause fixes, no workarounds. | Edited source files |

## Scripts

| Script | Purpose |
|--------|---------|
| `rechecker.py` | Hook entry point. Reads PostToolUse JSON, detects `git commit`, finds git roots, launches orchestrator via `claude --worktree` (Popen, non-blocking) |
| `log-stop-failure.py` | Logs StopFailure events (rate limits, server errors) for debugging |
| `scan.sh` | Optional: Runs Super-Linter, Semgrep, TruffleHog via Docker (not used by default) |
| `publish.py` | Dev tool: test, lint, bump version (including README badge), tag, push, create GitHub release |

## Configuration

The plugin works out of the box with no configuration. Key defaults:

| Setting | Default | Where |
|---------|---------|-------|
| Max passes per loop | 30 | `rechecker-orchestrator.md` |
| Hook timeout | 24 hours | `hooks/hooks.json` |
| Hook mode | async | `hooks/hooks.json` (`"async": true`) |
| Reviewer model | opus[1m] | Agent frontmatter |
| Fixer model | sonnet | `sonnet-code-fixer.md` frontmatter |
| Permission mode | bypass all | `--dangerously-skip-permissions` |

### Gitignore

Add these to your project's `.gitignore`:

```
.rechecker/
reports_dev/
```

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

## Requirements

| Requirement | Why |
|-------------|-----|
| `claude` CLI on PATH | Runs agents in headless mode via `claude --worktree` |
| `python3` (3.12+) on PATH | Hook detection script |
| `git` repository | Worktrees, diffs, commits |
| Max subscription | `claude --worktree` uses Max subscription auth |

## Cross-Platform Compatibility

| Platform | Status | Notes |
|----------|--------|-------|
| macOS | Supported | Python 3.12+ required |
| Linux | Supported | Python 3.12+ required |
| WSL | Supported | Works out of the box |
| Windows | Untested | Python 3.12+ required |

## Safety

| Concern | Mitigation |
|---------|------------|
| Git state corruption | All work in isolated worktree; merged once at end |
| Infinite loops | Max 30 passes per loop; orchestrator tracks progress |
| Concurrent reviews | Each git root gets its own named worktree |
| Report pollution | Reports go to `.rechecker/` (gitignored); not committed |

## License

MIT — see [LICENSE](LICENSE)

## Author

**Emasoft** — [GitHub](https://github.com/Emasoft)

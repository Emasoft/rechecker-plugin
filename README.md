# rechecker-plugin

<!--BADGES-START-->
[![CI](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-2.1.0-blue)](https://github.com/Emasoft/rechecker-plugin)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Validation](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml)
<!--BADGES-END-->

A Claude Code plugin that **automatically reviews and fixes code changes after every git commit**. It launches a 4-agent swarm in an isolated git worktree, iterating through lint checks, correctness reviews, and intent verification until the code is clean.

---

## The Problem

Claude Code often introduces bugs, incomplete implementations, or subtle regressions when making code changes. Without this plugin, you'd manually ask Claude to "check your changes for errors, then fix them" — sometimes repeating this **8+ times** before all issues are resolved.

## The Solution

This plugin automates the entire review-fix cycle. After every commit, it:

1. Detects the commit in the background (non-blocking)
2. Launches an orchestrator in an isolated git worktree
3. Runs 4 iterative loops (lint → correctness → intent → final lint)
4. Fixes all issues found using parallel agent swarms
5. Commits the fixes and merges back to your branch

You keep working while the review happens in the background.

---

## Installation

### From the Emasoft Marketplace (recommended)

First, add the marketplace (one-time setup):

```bash
claude plugin marketplace add Emasoft/emasoft-plugins
```

Then install the plugin:

```bash
claude plugin install rechecker-plugin@emasoft-plugins
```

### From GitHub (direct)

```bash
claude plugin install --source github Emasoft/rechecker-plugin
```

### Requirements

- **Claude CLI** on PATH — runs agents headless via `claude --worktree`
- **Python 3.12+** on PATH — hook detection script
- **git** — worktrees, diffs, commits
- **Max subscription** — `claude --worktree` requires Max subscription auth

---

## Usage

### Automatic Mode (default)

**No action needed.** The plugin activates automatically after every `git commit` command that Claude runs. It works in the background — you won't even notice it unless it finds and fixes issues.

What triggers it:
- Any `git commit` in a Bash command (including compound commands like `cd /repo && git commit -m "..."`)
- Multiple commits to different git repos in one command are handled separately

What does NOT trigger it:
- `git commit --amend` (skipped intentionally)
- Non-Bash tools (only Bash commands are monitored)

### Manual Mode (`/recheck`)

Run the review pipeline on demand:

```
/rechecker-plugin:recheck
```

This does exactly the same thing as the automatic hook, but you control when it runs. Useful for:
- Re-checking after a manual edit
- Running the pipeline on a commit that happened before the plugin was installed
- Verifying the code is clean before pushing

### Reading the Report

After each run, a report is saved to:

```
reports_dev/rechecker-report-{TIMESTAMP}.md
```

The report contains:
- Date and summary
- Total issues found and fixed
- Per-issue details: file, line, severity, description

---

## How It Works

```
You commit code
      │
      ▼
PostToolUse hook detects "git commit" ─── async: true (non-blocking)
      │
      ▼
rechecker.py finds all git roots in the command
      │
      ▼
For each repo: claude --worktree rechecker-{name}
      │
      ▼
┌─────────────────────────────────────────────┐
│          ORCHESTRATOR (opus[1m])             │
│                                             │
│  LOOP 1: Lint ──▶ sonnet fixes ──▶ repeat   │
│          until 0 lint errors                │
│                                             │
│  LOOP 2: Opus code review ──▶ sonnet fixes  │
│          ──▶ repeat until 0 bugs            │
│                                             │
│  LOOP 3: Opus intent review ──▶ sonnet fixes│
│          ──▶ repeat until 0 intent issues   │
│                                             │
│  LOOP 4: Final lint ──▶ sonnet fixes        │
│          ──▶ repeat until 0 lint errors     │
│                                             │
│  Merge reports ──▶ ONE commit ──▶ exit      │
└─────────────────────────────────────────────┘
      │
      ▼
Claude Code merges the worktree back to your branch
Report moved to reports_dev/
```

### The 4 Agents

| Agent | Model | What It Does |
|-------|-------|--------------|
| **rechecker-orchestrator** | opus[1m] | Coordinates all 4 loops. Spawns reviewer and fixer swarms in parallel. Makes exactly 1 commit at the end. |
| **opus-code-reviewer** | opus[1m] | Reviews one file for correctness bugs across 13 categories (logic errors, null handling, types, race conditions, resource leaks, security, etc.). Writes findings to JSON. Does NOT fix anything. |
| **opus-functionality-reviewer** | opus[1m] | Verifies one file does what the commit message claims. Checks 9 categories (intent mismatch, incomplete implementation, broken contracts, silent failures, etc.). Writes findings to JSON. Does NOT fix anything. |
| **sonnet-code-fixer** | sonnet | Reads a findings JSON file and applies root-cause fixes to the source file. No workarounds, no band-aids. Does NOT commit. |

### Inter-Agent Data Exchange

Agents exchange data through files, never inline in prompts:

```
.rechecker/
  files.txt                          # changed files list
  commit-message.txt                 # commit message for intent analysis
  reports/
    lint-pass{N}.txt                 # linter output per pass
    ocr-pass{N}-{SAFE_NAME}.json    # code review findings
    ofr-pass{N}-{SAFE_NAME}.json    # intent review findings
    scf-pass{N}-{SAFE_NAME}.md      # fix summaries
```

---

## Plugin Structure

```
rechecker-plugin/
├── .claude-plugin/
│   └── plugin.json                    # Plugin manifest
├── hooks/
│   └── hooks.json                     # PostToolUse (async) + StopFailure
├── agents/
│   ├── rechecker-orchestrator.md      # Opus orchestrator
│   ├── opus-code-reviewer.md          # Opus reviewer (correctness)
│   ├── opus-functionality-reviewer.md # Opus reviewer (intent)
│   └── sonnet-code-fixer.md           # Sonnet fixer
├── skills/
│   └── recheck/
│       └── SKILL.md                   # /recheck command
├── scripts/
│   ├── rechecker.py                   # Hook entry point
│   ├── log-stop-failure.py            # StopFailure logger
│   ├── scan.sh                        # Optional: Docker-based scanning
│   └── publish.py                     # Dev tool: bump, tag, release
├── .gitignore
└── README.md
```

---

## Configuration

The plugin works **out of the box** with no configuration needed.

| Setting | Default | Location |
|---------|---------|----------|
| Max passes per loop | 30 | `rechecker-orchestrator.md` |
| Hook timeout | 24 hours | `hooks/hooks.json` |
| Hook mode | async (non-blocking) | `hooks/hooks.json` |
| Reviewer model | opus[1m] | Agent frontmatter |
| Fixer model | sonnet | Agent frontmatter |
| Permission mode | bypass all | `--dangerously-skip-permissions` |

### Gitignore

Add these to your project's `.gitignore` (the plugin creates these directories):

```gitignore
.rechecker/
reports_dev/
```

---

## Multi-Repo Support

The plugin handles monorepos and compound commands with multiple git repos. If a single Bash command commits to multiple repos:

```bash
cd /project-a && git commit -m "fix A" && cd /project-b && git commit -m "fix B"
```

The hook detects both commits, finds each git root, and launches a **separate orchestrator worktree for each repo**. Each repo is reviewed independently and in parallel.

Git submodules are also supported — the hook uses `git rev-parse --show-toplevel` for robust git root detection.

---

## Safety

| Concern | How It's Handled |
|---------|-----------------|
| **Git state** | All work happens in an isolated worktree — your working tree is never touched |
| **Infinite loops** | Max 30 passes per loop; orchestrator exits if issues don't converge |
| **Concurrent reviews** | Each git root gets its own named worktree — no conflicts |
| **Non-blocking** | Hook runs async — you keep working while the review happens |
| **Worktree cleanup** | Claude Code automatically cleans up worktrees at session end |
| **Report persistence** | Report is committed in the worktree, merged to main, then moved to `reports_dev/` |

---

## Cross-Platform Support

| Platform | Status |
|----------|--------|
| macOS | Supported |
| Linux | Supported |
| WSL | Supported |
| Windows | Untested |

Python 3.12+ required on all platforms.

---

## License

MIT — see [LICENSE](LICENSE)

## Author

**Emasoft** — [GitHub](https://github.com/Emasoft)

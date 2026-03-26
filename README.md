# rechecker-plugin

<!--BADGES-START-->
[![CI](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-3.0.1-blue)](https://github.com/Emasoft/rechecker-plugin)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Validation](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml)
<!--BADGES-END-->

A Claude Code plugin that **automatically reviews and fixes code changes after every git commit**. It launches a review pipeline in an isolated git worktree, iterating through lint checks, correctness reviews, and intent verification until the code is clean.

---

## The Problem

Claude Code often introduces bugs, incomplete implementations, or subtle regressions when making code changes. Without this plugin, you'd manually ask Claude to "check your changes for errors, then fix them" — sometimes repeating this **8+ times** before all issues are resolved.

## The Solution

This plugin automates the entire review-fix cycle. After every commit, it:

1. Detects the commit in the background (non-blocking)
2. Launches an orchestrator in an isolated git worktree
3. Runs 4 iterative loops (lint → correctness → intent → final lint)
4. Reviews code via LLM Externalizer (grok/gemini on OpenRouter — not Claude tokens)
5. Fixes all issues using parallel sonnet-code-fixer agents
6. Commits the fixes and notifies Claude to merge

You keep working while the review happens in the background.

---

## Installation

### From the Emasoft Marketplace (recommended)

First, add the marketplace (one-time setup):

```bash
claude plugin marketplace add emasoft-plugins
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
- **LLM Externalizer plugin** — reviews are externalized to OpenRouter (grok/gemini)
  ```bash
  claude plugin install llm-externalizer@emasoft-plugins
  ```

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
- Commands inside a rechecker worktree (recursion guard)

### After the Review Completes

When the rechecker finishes, Claude receives a prominent notification telling it to merge. The merge script handles everything automatically:

```bash
bash .rechecker/merge-worktrees.sh
```

This script:
- Removes worktrees (can't merge a checked-out branch)
- Auto-stashes dirty working tree
- Merges each branch with `-X ours` (current branch wins on conflict)
- Moves reports to `docs_dev/`
- Deletes merged branches
- Auto-commits cleanup
- Restores stash

Options: `--dry-run` (preview), `--no-cleanup` (skip branch/file deletion)

### Manual Mode (`/recheck`)

Run the review pipeline on demand:

```
/rechecker-plugin:recheck
```

### Resume After Interruption

If Claude gets rate-limited or the session is interrupted, the plugin automatically detects pending work when the session resumes (`SessionStart[resume]` hook). It tells Claude about:
- **Pending merges** — completed reviews waiting to be merged
- **Incomplete runs** — worktrees interrupted mid-pipeline, with progress state for resume

Progress is tracked atomically in `.rechecker/rck-progress.json`, so the orchestrator can skip completed loops and resume from where it stopped.

### Reading the Reports

After merging, reports are saved to:

```
docs_dev/rck-{TIMESTAMP}_{UID}-report.md
```

The report contains:
- Date and summary
- Total issues found and fixed per loop
- Per-issue details: file, severity, description, fix applied

---

## How It Works

```
You commit code
      │
      ▼
PostToolUse[Bash] hook detects "git commit" ─── async (non-blocking)
      │
      ▼
rechecker.py finds all git roots, copies merge-worktrees.sh to .rechecker/
      │
      ▼
For each repo: claude --worktree rck-{uid}
      │
      ▼
┌──────────────────────────────────────────────────────┐
│          ORCHESTRATOR (sonnet)                        │
│                                                      │
│  Progress: .rechecker/rck-progress.json (atomic)     │
│                                                      │
│  LOOP 1: Lint ──▶ sonnet-code-fixer ──▶ repeat       │
│          until 0 lint errors                         │
│                                                      │
│  LOOP 2: LLM Externalizer review (correctness)      │
│          ──▶ sonnet-code-fixer ──▶ repeat            │
│          until 0 bugs                                │
│                                                      │
│  LOOP 3: LLM Externalizer review (intent vs reality)│
│          ──▶ sonnet-code-fixer ──▶ repeat            │
│          until 0 intent issues                       │
│                                                      │
│  LOOP 4: Final lint ──▶ sonnet-code-fixer            │
│          ──▶ repeat until 0 lint errors              │
│                                                      │
│  Merge reports ──▶ ONE commit ──▶ exit               │
└──────────────────────────────────────────────────────┘
      │
      ▼
rechecker.py copies report, writes merge-pending notice
      │
      ▼
Claude receives additionalContext: "MERGE THE FIXES NOW"
      │
      ▼
Claude runs: bash .rechecker/merge-worktrees.sh
      │
      ▼
Reports in docs_dev/. Done.
```

### Components

| Component | Model/Tool | What It Does |
|-----------|------------|--------------|
| **rechecker.py** | Python | PostToolUse hook. Detects `git commit`, launches worktree, copies reports, notifies Claude. |
| **rechecker-orchestrator** | sonnet | Coordinates all 4 loops. Uses pipeline.py for progress tracking and report merging. |
| **LLM Externalizer MCP** | grok/gemini (OpenRouter) | Reviews each file for correctness bugs and intent mismatches. NOT Claude tokens. |
| **sonnet-code-fixer** | sonnet | Agent swarm worker. Reads findings, applies root-cause fixes to source files. |
| **pipeline.py** | Python | CLI helper for file grouping, progress tracking (atomic writes), and report merging. |
| **merge-worktrees.sh** | bash | Standalone merge script. Handles stash, merge, cleanup, branch deletion. |
| **resume-check.py** | Python | SessionStart[resume] hook. Detects pending merges and incomplete runs after interruptions. |
| **log-stop-failure.py** | Python | StopFailure hook. Logs API errors (rate limits, server errors). |

### Inter-Agent Data Exchange

Agents exchange data through files, never inline in prompts:

```
.rechecker/
  files.txt                                    # changed files list
  commit-message.txt                           # commit message for intent analysis
  rck-progress.json                            # atomic progress tracking
  merge-worktrees.sh                           # standalone merge script (copied from plugin)
  reports/
    lint-pass{N}.txt                           # linter output per pass
    rck-{TS}_{UID}-[LP-IT-FID]-review.md       # LLM Externalizer review findings
    rck-{TS}_{UID}-[LP-IT-FID]-fix.md          # sonnet-code-fixer fix reports
    rck-{TS}_{UID}-[LP-IT]-iteration.md        # merged iteration report
    rck-{TS}_{UID}-[LP]-loop.md                # merged loop report
```

---

## Plugin Structure

```
rechecker-plugin/
├── .claude-plugin/
│   └── plugin.json                    # Plugin manifest
├── hooks/
│   └── hooks.json                     # PostToolUse + SessionStart + StopFailure
├── agents/
│   ├── rechecker-orchestrator.md      # Sonnet orchestrator (4 loops + progress)
│   ├── opus-code-reviewer.md          # Legacy (unused — reviews via LLM Externalizer)
│   ├── opus-functionality-reviewer.md # Legacy (unused — reviews via LLM Externalizer)
│   └── sonnet-code-fixer.md           # Sonnet fixer agent
├── skills/
│   └── recheck/
│       └── SKILL.md                   # /recheck command
├── scripts/
│   ├── rechecker.py                   # Hook entry point (PostToolUse)
│   ├── resume-check.py                # Resume hook (SessionStart)
│   ├── pipeline.py                    # Pipeline helper (index, groups, progress, merging)
│   ├── merge-worktrees.sh             # Standalone merge script (git+bash only)
│   ├── log-stop-failure.py            # StopFailure logger
│   ├── publish.py                     # Dev tool: lint, bump, tag, release
│   └── scan.sh                        # Optional: Docker-based scanning
├── .gitignore
└── README.md
```

---

## Configuration

The plugin works **out of the box** with no configuration needed.

| Setting | Default | Location |
|---------|---------|----------|
| Max passes per loop | 5 | `rechecker-orchestrator.md` |
| Hook timeout | 2 hours | `hooks/hooks.json` |
| Hook mode | async (non-blocking) | `hooks/hooks.json` |
| Orchestrator model | sonnet | Agent frontmatter |
| Fixer model | sonnet | Agent frontmatter |
| Review engine | LLM Externalizer (OpenRouter) | Orchestrator instructions |
| Permission mode | bypass all | `--dangerously-skip-permissions` |

### Gitignore

The plugin automatically adds `.rechecker/` entries to your project's `.gitignore`. You should also add:

```gitignore
docs_dev/
```

---

## Multi-Repo Support

The plugin handles monorepos and compound commands with multiple git repos. If a single Bash command commits to multiple repos:

```bash
cd /project-a && git commit -m "fix A" && cd /project-b && git commit -m "fix B"
```

The hook detects both commits, finds each git root, and launches a **separate orchestrator worktree for each repo**. Each repo is reviewed independently and in parallel.

Git submodules are also supported — the hook uses `git rev-parse --show-toplevel` for robust git root detection, and `--ignore-submodules` in dirty checks.

---

## Safety

| Concern | How It's Handled |
|---------|-----------------|
| **Git state** | All work happens in an isolated worktree — your working tree is never touched |
| **Infinite loops** | Max 5 passes per loop; orchestrator exits if issues don't converge |
| **Recursion** | Branch name check prevents rechecker from triggering inside its own worktrees |
| **Concurrent reviews** | Each commit gets a unique worktree (UUID-based) — no conflicts |
| **Non-blocking** | Hook runs async — you keep working while the review happens |
| **Interruption** | Progress tracked atomically; SessionStart[resume] hook detects incomplete runs |
| **TLDR artifacts** | Automatically gitignored in target projects to prevent commit pollution |
| **Report persistence** | Reports moved to docs_dev/ during merge — never lost |

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

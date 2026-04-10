# rechecker-plugin

<!--BADGES-START-->
[![CI](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-3.2.15-blue)](https://github.com/Emasoft/rechecker-plugin)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Validation](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml/badge.svg)](https://github.com/Emasoft/rechecker-plugin/actions/workflows/validate.yml)
<!--BADGES-END-->

A Claude Code plugin that **reviews and fixes code after git commits** using a blocking `/recheck` skill with multiple review passes.

---

## The Problem

Claude Code often introduces bugs, incomplete implementations, or subtle regressions when making code changes. Without this plugin, you'd manually ask Claude to "check your changes for errors, then fix them" — sometimes repeating this multiple times before all issues are resolved.

## The Solution

This plugin adds a `/recheck` skill that runs a structured review-fix pipeline on committed code. A rule tells Claude to invoke it automatically after significant commits (5+ files or 50KB+). The pipeline runs inline and blocking — no worktrees, no background processes.

---

## Installation

### From the Emasoft Marketplace (recommended)

```bash
claude plugin marketplace add emasoft-plugins
claude plugin install rechecker-plugin@emasoft-plugins
```

### From GitHub (direct)

```bash
claude plugin install --source github Emasoft/rechecker-plugin
```

### Requirements

- **Python 3.12+** on PATH
- **git**
- **LLM Externalizer plugin** — reviews are sent to external LLMs (grok/gemini via OpenRouter)
  ```bash
  claude plugin install llm-externalizer@emasoft-plugins
  ```

---

## Usage

### Automatic Mode (default)

A rule instructs Claude to run `/recheck` after any commit with **5 or more files** or **50 KB+ total size**. Commits with `[rechecker: skip]` in the message are ignored (recursion guard).

### Manual Mode

Run the review pipeline on demand:

```
/rechecker-plugin:recheck
```

---

## How It Works

```
You commit code
      |
      v
Claude checks the rule: >=5 files or >=50KB?
      |
      v (yes)
/recheck skill runs (blocking)
      |
      v
triage.py (one script does all mechanical work)
  |- Recursion guard check
  |- Detect changed files, filter non-code, classify by size
  |- Split into groups (max 10 files each, by extension family)
  |- Run linters (ruff/mypy/eslint/tsc/shellcheck/etc.)
  |- Filter lint errors (Python, no haiku agent needed)
  |- Detect security-relevant groups
  |- Write per-group JSON files + token snapshot
  |- Output compact manifest with ---GROUP:id--- markers
      |
      v
Orchestrator reads manifest, dispatches:
  |- Lint fix: sonnet-code-fixer per group with errors
  |- Pass 1-3: one code_task call with GROUP markers → per-group reports
  |- Pass 4 (security): only security-relevant groups
  |- Large files (>250KB): opus agent per file
  |- Each pass: sonnet-code-fixer for groups with issues
      |
      v
Commit fixes with [rechecker: skip] marker
      |
      v
finalize-session.py → count tokens, write history, move reports
      |
      v
Done. Summary reported to user.
```

### Components

| Component | What It Does |
|-----------|-------------|
| **`/recheck` skill** | The full pipeline — triage, lint, 3+1 review passes, commit, finalize |
| **`triage.py`** | Detects files, lints, classifies, splits into groups, outputs manifest |
| **`sonnet-code-fixer`** | Sonnet agent that fixes reported issues using Serena MCP |
| **LLM Externalizer** | External LLM (grok/gemini) reviews code for bugs — not Claude tokens |
| **`finalize-session.py`** | Automates: token counting, session history, report cleanup |
| **`count-tokens.py`** | Parses JSONL transcripts for per-model token breakdown (snapshot/delta/transcripts modes) |
| **`log-subagent-tokens.py`** | SubagentStop hook — logs isolated token usage per subagent/worktree |
| **`log-stop-failure.py`** | Logs API errors (rate limits, server errors, auth failures) |
| **`recheck-after-commit` rule** | Tells Claude when to trigger `/recheck` |

### Review Pass Details

| Pass | Focus | Checks |
|------|-------|--------|
| **Lint** | Static analysis | Syntax errors, type errors, import errors |
| **1** | Code correctness | Logic bugs, off-by-one, race conditions, deprecated APIs |
| **2** | Functional correctness | Edge cases, return values, async handling, API contracts |
| **3** | Adversarial | Injection, resource exhaustion, state corruption, type confusion |
| **4** | Security (conditional) | SQL/XSS/command injection, auth, secrets, crypto |

### File Size Routing

| Size | Reviewer |
|------|----------|
| <=250KB | LLM Externalizer (cheap, fast) |
| 250-500KB | Opus[1m] agent (large context) |
| >500KB | Skipped |

### Session Tracking

Each recheck run gets a unique UUID. A session record is appended to `.rechecker/history.jsonl`:

```json
{"uuid":"a1b2c3d4e5f6","commit":"abc123...","started":"...","finished":"...","files_reviewed":8,"issues_found":3,"issues_fixed":3,"commit_made":true,"tokens":{...}}
```

Reports are stored in `.rechecker/reports/<uuid>/` (gitignored).

---

## Plugin Structure

```
rechecker-plugin/
├── .claude-plugin/
│   └── plugin.json              # Plugin manifest
├── hooks/
│   └── hooks.json               # StopFailure + SubagentStop hooks
├── agents/
│   ├── sonnet-code-fixer.md     # Sonnet fixer (Serena MCP + Grepika)
│   └── lint-filter.md           # Haiku lint output filter (fallback)
├── rules/
│   └── recheck-after-commit.md  # Auto-trigger rule
├── skills/
│   └── recheck/
│       ├── SKILL.md             # /recheck pipeline orchestration
│       └── review-passes.md     # Review instructions per pass
├── scripts/
│   ├── triage.py                # File detection, lint, classify, group split
│   ├── finalize-session.py      # Token counting + history + cleanup
│   ├── count-tokens.py          # JSONL transcript parser
│   ├── log-subagent-tokens.py   # SubagentStop token logger
│   ├── log-stop-failure.py      # API error logger
│   └── publish.py               # Dev: lint, validate, bump, tag, release
└── README.md
```

---

## Safety

| Concern | How It's Handled |
|---------|-----------------|
| **Recursion** | Commits include `[rechecker: skip]` marker; rule and skill both check it |
| **Token waste** | Lint filtered by haiku; reviews via external LLM; finalize automated by script |
| **Code deletion** | All review prompts include "NEVER suggest removing code" rules |
| **Fixer scope** | "Fix ONLY what is reported. When in doubt, SKIP." |
| **Cleanup** | Reports moved to `.rechecker/` (gitignored); temp files removed |
| **Blocking** | Runs inline — no background processes, no worktrees, no orphaned branches |

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

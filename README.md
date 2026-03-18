# rechecker-plugin

A Claude Code plugin that automatically reviews and fixes code changes after git commits, using a separate Claude instance in an isolated git worktree.

## How It Works

1. **Trigger**: After Claude runs `git commit`, the PostToolUse hook detects it
2. **Worktree**: Creates an isolated git worktree for the review
3. **Review**: Runs `claude --agent code-reviewer` (headless) to review the diff and fix issues
4. **Merge**: If fixes are made, merges them back into the working branch
5. **Loop**: Repeats until zero issues are found or max 5 passes reached
6. **Report**: Saves detailed reports to `reports_dev/`

## Installation

### From local directory

```bash
# Symlink the plugin
ln -s /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin

# Or copy it
cp -r /path/to/rechecker-plugin ~/.claude/plugins/rechecker-plugin
```

### Verify installation

Restart Claude Code, then type `/hooks` to confirm the PostToolUse hook appears.

## Usage

Once installed, the plugin works automatically. Whenever Claude commits code, the rechecker:

1. Detects the `git commit` command
2. Gets the diff of the commit
3. Creates a worktree and runs a fresh Claude instance to review
4. If issues are found and fixed, merges the fixes back
5. Repeats until the reviewer finds zero issues (max 5 passes)
6. Injects a summary into Claude's context with links to the reports

### Reports

Reports are saved to `<project>/reports_dev/` with the naming convention:
```
rechecker_<timestamp>_pass<N>.md    # Per-pass report
rechecker_<timestamp>_summary.md   # Final summary
```

## Configuration

The plugin works out of the box with no configuration needed.

### Gitignore

Add these to your project's `.gitignore`:
```
.rechecker/
reports_dev/
```

## Requirements

- **Claude Code CLI** (`claude`) must be available on PATH
- **python3** must be available (for JSON parsing in hook scripts)
- **git** repository with at least one commit
- The project must be a git repo (plugin is a no-op otherwise)

## What the Reviewer Checks

- **Correctness**: Logic errors, null handling, type mismatches, edge cases
- **Security**: Injection vulnerabilities, path traversal, hardcoded secrets
- **Error Handling**: Swallowed exceptions, missing validation
- **API Contracts**: Breaking changes, missing return values

## What the Reviewer Ignores

- Code style and formatting (handled by linters)
- Performance unless obviously algorithmic
- Feature suggestions
- Refactoring preferences

## Limitations

- Maximum 5 review passes per commit
- Total review timeout: 15 minutes
- Does not trigger on `git commit --amend`
- Concurrent commits skip review if another is in progress
- Merge conflicts during fix merge abort the review loop

## Plugin Structure

```
rechecker-plugin/
├── .claude-plugin/
│   └── plugin.json              # Plugin manifest
├── hooks/
│   └── hooks.json               # PostToolUse hook on Bash
├── agents/
│   └── code-reviewer.md         # Agent definition with review checklist
├── scripts/
│   ├── rechecker.sh             # Entry point: detect commit, lock, invoke loop
│   └── review-loop.sh           # Core: worktree create -> review -> merge -> destroy
└── README.md
```

## License

MIT

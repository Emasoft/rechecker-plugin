# Recheck - On-Demand Code Review

Trigger the rechecker review loop manually on the latest commit (or a specified commit). This does the same thing the PostToolUse hook does automatically after git commits, but on demand.

## Usage

```
/recheck              # Review the latest commit on the current branch
/recheck <commit_sha> # Review a specific commit
```

## What It Does

1. Resolves the target commit (HEAD or the provided SHA)
2. Acquires the rechecker lock (skips if another review is running)
3. Runs the full review loop: worktree creation, scan.sh, code review, fix, merge, repeat
4. Saves reports to `reports_dev/`
5. Returns a summary with a pointer to the report files

## Instructions

Run the rechecker review loop on demand. This is identical to what the PostToolUse hook triggers after a `git commit`.

If the user provided a commit SHA as an argument, pass it to the script. Otherwise, the script defaults to HEAD.

Run this command via the Bash tool:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recheck.py" <COMMIT_SHA_OR_EMPTY>
```

Replace `<COMMIT_SHA_OR_EMPTY>` with the user-provided commit SHA, or omit it entirely to review HEAD.

After the script completes, READ the summary report file mentioned in the output to see the full results.

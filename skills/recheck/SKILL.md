---
name: recheck
description: Review and fix the last committed code changes
---

Automated code review and fix pipeline for the latest commit. Runs inline (no worktrees), blocking.

## Recursion guard

Before doing anything, check if the latest commit is already a rechecker commit:
```bash
git log -1 --format=%s | grep -q '\[rechecker: skip\]' && echo "SKIP" || echo "PROCEED"
```
If it prints `SKIP`, stop immediately — this commit was made by the rechecker itself.

## Step 1: Identify changed files

```bash
git show --name-only --format= --diff-filter=d HEAD
```

Filter out non-code files. Skip files matching these patterns:
- Media: `*.png, *.jpg, *.jpeg, *.gif, *.svg, *.ico, *.mp3, *.mp4, *.webm, *.webp, *.avif, *.bmp, *.tiff, *.pdf, *.eps, *.ai`
- Data/config: `*.csv, *.tsv, *.parquet, *.sqlite, *.db, *.lock, *.lockb`
- Generated: `CHANGELOG.md, LICENSE, *.min.js, *.min.css, *.map, *.bundle.js, *.chunk.js`
- Docs: `*.md` (except README.md)
- Binary: `*.whl, *.tar.gz, *.zip, *.egg, *.so, *.dylib, *.dll, *.exe, *.bin`
- Fonts: `*.woff, *.woff2, *.ttf, *.otf, *.eot`

Also skip files larger than 500KB:
```bash
wc -c < <file>
```

If no code files remain after filtering, stop — nothing to review.

## Step 2: Review with LLM Externalizer

For each code file (or batched together if small), use `mcp__plugin_llm-externalizer_llm-externalizer__code_task` to review:

**Instructions** (pass as `instructions` parameter):
```
Review this code file for bugs, security vulnerabilities, logic errors, and correctness issues.

For each issue found, report:
### BUG: <short title>
- **File**: <filename>
- **Line**: <line number or range>
- **Severity**: critical / high / medium / low
- **Description**: What is wrong
- **Fix**: What should be changed

CRITICAL RULES — violations break the build:
- NEVER suggest removing code you think is "unused" — it may be used by other files
- NEVER suggest removing variables, imports, functions, or classes unless they cause an error
- NEVER suggest style-only changes (formatting, naming, reordering)
- Only report actual bugs, security holes, or logic errors
- If the code is correct, say "No issues found"
```

Pass the file path via `input_files_paths`. Set `ensemble: true` for thorough review.

Read the output file to get the review results.

## Step 3: Fix issues (if any)

If issues were found, use the `rechecker-plugin:sonnet-code-fixer` agent to fix them.

For each file with issues, spawn one fixer agent:
- Pass the file path and the specific issues from the review
- Tell the fixer: "Fix ONLY these reported issues. Do NOT delete any code. Do NOT make style changes."

Use Serena MCP (`find_symbol`, `replace_symbol_body`) and TLDR for surgical edits.

If no issues were found in any file, skip to the summary step.

## Step 4: Commit fixes (recursion-safe)

Stage ONLY the files that were fixed (not `git add -A`):
```bash
git add <file1> <file2> ...
```

Commit with the rechecker skip marker so the hook does not trigger another recheck:
```bash
git commit -m "$(cat <<'EOF'
fix: apply rechecker fixes [rechecker: skip]

Auto-reviewed and fixed by rechecker plugin.
EOF
)"
```

## Step 5: Summary

Report to the user:
- How many files were reviewed
- How many issues were found (by severity)
- What was fixed
- Whether a commit was made

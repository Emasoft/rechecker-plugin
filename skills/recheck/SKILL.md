---
name: recheck
description: Review and fix the last committed code changes
---

Automated code review and fix pipeline for the latest commit. Runs inline, blocking. Three mandatory passes plus one conditional security pass.

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

Also skip files larger than 500KB.

Split the remaining files into two groups by size:
- **Normal files** (≤250KB): reviewed via LLM Externalizer
- **Large files** (>250KB, ≤500KB): reviewed via a dedicated opus agent (see below)

If no code files remain after filtering, stop — nothing to review.

## Step 2: Setup report folder

Record the start timestamp (for token counting later) and create the report folder:
```bash
RCK_START_TS=$(date -u +%Y-%m-%dT%H:%M:%S)
REPORT_DIR="reports_dev/rck-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$REPORT_DIR"
```

All review outputs and fix reports go in this folder.

## Step 3: Review-fix passes

Each pass reviews the code, then fixes any issues found before moving to the next pass.

**For normal files (≤250KB):** send to `mcp__plugin_llm-externalizer_llm-externalizer__code_task` with `ensemble: false`.

**For large files (>250KB):** spawn a general-purpose agent with `model: opus` for each file. Pass the file path and the same review instructions, plus these additional instructions for the opus agent:

```
You are reviewing a large file. A sonnet-level fixer agent will apply your fixes, so your findings must be precise and actionable — do not leave anything ambiguous.

For each issue, you MUST provide:
1. The exact function/class/symbol name where the bug lives (if inside a symbol body)
2. The exact line number or line range (if the bug is in module-level code, global scope, or outside any function body)
3. A code snippet showing the CURRENT broken code (quote it exactly as it appears)
4. A code snippet showing the FIXED version (ready to paste — the fixer will use this directly)
5. A one-sentence explanation of WHY this is a bug

Use this format strictly:
### BUG: <title>
- **File**: <filename>
- **Symbol**: <function/class name> (or "module-level" if not inside a function)
- **Line**: <exact line number or range>
- **Current code**: `<exact broken code>`
- **Fixed code**: `<exact corrected code>`
- **Why**: <one sentence>
- **Severity**: critical / high / medium / low
```

The agent should write findings to `$REPORT_DIR/pass<N>-large-<filename>-review.md`. These agents can run in parallel with the LLM Externalizer call.

After collecting all review results (from both LLM Externalizer and opus agents), if issues were found, spawn **one** `rechecker-plugin:sonnet-code-fixer` agent to fix them.

When spawning the fixer agent, always pass:
- The list of file paths with issues
- The path(s) to the review output file(s)
- A report file path for the fix summary: `$REPORT_DIR/pass<N>-fixes.md`
- "Fix ONLY these reported issues. Do NOT delete any code. Do NOT make style changes."

All three passes share these rules appended to their instructions:

```
CRITICAL RULES:
- NEVER suggest removing code you think is "unused" — it may be used by other files
- NEVER suggest removing variables, imports, functions, or classes unless they cause an error
- NEVER suggest style-only changes (formatting, naming, reordering)
- If the code is correct for this pass, say "No issues found"

For each issue found, report:
### BUG: <short title>
- **File**: <filename>
- **Line**: <line number or range>
- **Severity**: critical / high / medium / low
- **Description**: What is wrong
- **Fix**: What should be changed
```

Copy each LLM Externalizer output file to `$REPORT_DIR/pass<N>-review.md` after reading it.

---

### Pass 1 — Code correctness

Instructions:

```
Check this code for correctness issues:
- Syntax errors, typos, malformed expressions
- Logic errors: wrong conditions, off-by-one, inverted checks, unreachable branches
- Race conditions: TOCTOU, shared mutable state without synchronization
- Outdated patterns: deprecated APIs, removed stdlib functions, obsolete idioms
- Inconsistencies: mismatched types, conflicting return values, broken contracts between functions
```

If issues found → spawn fixer, wait for completion.

---

### Pass 2 — Functional correctness

Instructions:

```
Check whether this code does what it is supposed to do:
- Does each function fulfill its documented purpose (docstring, name, comments)?
- Are edge cases handled: empty input, zero, negative, null, boundary values?
- Are return values correct in all branches?
- Do error paths clean up resources and leave consistent state?
- Are API contracts honored: correct HTTP methods, status codes, headers, payloads?
- Do loops terminate? Are iterators consumed correctly?
- Are async operations awaited? Are promises handled?
```

If issues found → spawn fixer, wait for completion.

---

### Pass 3 — Adversarial review

Instructions:

```
Review this code with an adversarial stance. Think like an attacker, a hostile user, a malicious dependency.

Input manipulation:
- What happens with empty string, null, undefined, NaN, Infinity, -0?
- What if input is 10GB, negative, or contains Unicode RTL characters?
- Can I inject SQL, shell commands, HTML, regex, template literals, path traversal (..)?

Resource exhaustion:
- Can I cause unbounded memory growth, hold a lock forever, trigger O(n^2)?
- What if disk is full, network drops, or a timeout never fires?

State corruption:
- What if this function is called in the wrong order or with stale state?
- What if config is malformed, truncated, or missing?
- What if environment variables are unset or unexpected?

Type confusion:
- What if a number is actually a string, an array has holes, or a Promise is passed as a value?
- Is there prototype pollution risk?

Dependency trust:
- Does this trust data from external APIs, files, or URLs without validation?
- Does this trust user-controlled filenames or paths?

Error path abuse:
- Do all error handlers clean up correctly?
- Can a half-written state be caused by interrupting at the worst moment?
- Do error messages leak sensitive information (paths, keys, tokens)?
```

If issues found → spawn fixer, wait for completion.

---

### Pass 4 — Security audit (conditional)

**Run this pass ONLY if** any of the changed files involve:
- Network/HTTP handling (servers, routes, middleware, API clients, fetch, requests)
- Authentication or authorization (login, tokens, sessions, passwords, OAuth, JWT)
- User input processing (forms, CLI args, query params, file uploads, deserialization)
- Database operations (queries, ORM, migrations)
- File system operations with user-controlled paths
- Shell/subprocess execution with dynamic arguments
- Prompt construction for LLMs (prompt injection risk)
- Cryptography or secret management
- Serialization/deserialization (JSON, YAML, pickle, XML)

If none of the files touch these areas, skip Pass 4 entirely.

Instructions:

```
Perform a security audit of this code. You are a penetration tester reviewing for exploitable vulnerabilities.

Injection attacks:
- SQL injection: are queries parameterized or is string concatenation used?
- Command injection: are shell commands built with user input? Is subprocess called with shell=True?
- Path traversal: can user input escape the intended directory via ../ or symlinks?
- XSS: is user input rendered in HTML without escaping?
- Template injection: is user input interpolated into templates (Jinja2, f-strings used as templates)?
- Prompt injection: is user input concatenated into LLM prompts without sanitization?
- LDAP/XPath/regex injection: is user input used in structured queries?

Authentication and authorization:
- Are credentials stored in plaintext? Are passwords hashed with a weak algorithm?
- Is session management secure? Are tokens validated server-side?
- Are authorization checks present on every privileged endpoint?
- Are JWTs verified with proper algorithm restrictions (no "alg: none")?

Input validation and sanitization:
- Is input validated at the boundary (type, length, range, format)?
- Are file uploads checked for type, size, and content?
- Is deserialization safe (no pickle.loads on untrusted data, no yaml.load without SafeLoader)?

Secrets and data exposure:
- Are API keys, tokens, or passwords hardcoded in source?
- Do error messages, logs, or stack traces expose internal paths, credentials, or PII?
- Are secrets passed via environment variables rather than command-line arguments?

Cryptography:
- Are deprecated algorithms used (MD5, SHA1 for security, DES, RC4)?
- Is randomness cryptographically secure (secrets module, not random)?
- Are TLS connections verified (no verify=False, no disabled certificate checks)?
```

If issues found → spawn fixer, wait for completion.

---

## Step 4: Commit fixes (recursion-safe)

If no files were changed by any fixer across all passes, skip this step — nothing to commit.

Stage ONLY the files that were fixed (not `git add -A`):
```bash
git add <file1> <file2> ...
```

Commit with the rechecker skip marker to prevent recursion:
```bash
git commit -m "$(cat <<'EOF'
fix: apply rechecker fixes [rechecker: skip]

Auto-reviewed and fixed by rechecker plugin.
EOF
)"
```

## Step 5: Token usage report

Run the token counter to measure how much the recheck cost. The `$RCK_START_TS` variable was recorded in Step 2.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/count-tokens.py" --since "$RCK_START_TS"
```

Save the output to `$REPORT_DIR/token-usage.json`.

## Step 6: Summary

Report to the user:
- How many files were reviewed
- Issues found per pass (correctness / functional / adversarial / security) with severity counts
- What was fixed, what was skipped
- Whether Pass 4 (security) was triggered and why
- Whether a commit was made
- **Token usage**: total tokens, estimated cost, breakdown by model (from token-usage.json)
- Location of reports: `$REPORT_DIR/`

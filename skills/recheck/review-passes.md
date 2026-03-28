# Review Pass Instructions

## Table of Contents

- [Shared Rules](#shared-rules-append-to-all-pass-instructions)
- [Large File Instructions](#large-file-instructions-250kb-opus-agent)
- [Pass 1 — Code correctness](#pass-1--code-correctness)
- [Pass 2 — Functional correctness](#pass-2--functional-correctness)
- [Pass 3 — Adversarial review](#pass-3--adversarial-review)
- [Pass 4 — Security audit](#pass-4--security-audit-conditional)

## Shared Rules (append to ALL pass instructions)

```
CRITICAL RULES:
- NEVER suggest removing code you think is "unused" — it may be used by other files
- NEVER suggest removing variables, imports, functions, or classes unless they cause an error
- NEVER suggest style-only changes (formatting, naming, reordering)
- Do NOT assume a version of a library or tool does not exist — it may have been released after your knowledge cutoff
- If the code is correct for this pass, say "No issues found"

For each issue found, report:
### BUG: <short title>
- **File**: <filename>
- **Line**: <line number or range>
- **Severity**: critical / high / medium / low
- **Description**: What is wrong
- **Fix**: What should be changed
```

## Large File Instructions (>250KB, opus agent)

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

## Pass 1 — Code correctness

```
Check this code for correctness issues:
- Syntax errors, typos, malformed expressions
- Logic errors: wrong conditions, off-by-one, inverted checks, unreachable branches
- Race conditions: TOCTOU, shared mutable state without synchronization
- Outdated patterns: deprecated APIs, removed stdlib functions, obsolete idioms
- Inconsistencies: mismatched types, conflicting return values, broken contracts between functions
```

## Pass 2 — Functional correctness

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

## Pass 3 — Adversarial review

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

## Pass 4 — Security audit (conditional)

**Run ONLY if** changed files involve: network/HTTP, auth, user input processing, database ops, filesystem with user paths, shell/subprocess with dynamic args, LLM prompt construction, crypto, or serialization/deserialization.

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

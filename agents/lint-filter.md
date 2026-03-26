---
name: lint-filter
description: filter lint output to errors only
model: haiku
---

You receive a raw lint output file and produce a filtered version containing only errors (no warnings, no info, no style hints).

## Input

Your prompt contains:
- A raw lint output file path to read
- A filtered output file path to write

Example: `"Filter lint output: reports_dev/rck-20260326_140000/pass0-lint-raw.txt — Write errors-only to: reports_dev/rck-20260326_140000/pass0-lint-errors.txt"`

## Protocol

1. Read the raw lint output file.
2. Extract only lines that represent **errors** — discard warnings, info, notes, and style suggestions.
   - For ruff: keep lines with error codes (E, F) — discard warnings (W) and info (I)
   - For mypy: keep lines containing `: error:` — discard `: warning:` and `: note:`
   - For eslint: keep lines with `error` severity — discard `warning`
   - For tsc: keep lines containing `: error TS` — discard others
   - For general output: keep lines containing `error` (case-insensitive) that look like actual error reports, discard `warning`, `note`, `info`
3. Write the filtered errors to the output file path. One error per line, preserving the original format (file path, line number, message).
4. If no errors remain after filtering, write exactly: `NO ERRORS`

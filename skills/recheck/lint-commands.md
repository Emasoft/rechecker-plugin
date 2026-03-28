# Lint Commands Reference

## Table of Contents

- [Overview](#lint-commands-reference)
- [Commands](#commands)

## Commands

Group changed files by extension and run the matching linter. Skip linters not installed. Use `uvx` (Python) and `bunx` (JS) for zero-install execution.

```bash
# Python (.py)
uvx ruff check <files> >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null
uvx mypy <files> --ignore-missing-imports >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null
true

# JavaScript/TypeScript (.js, .ts, .jsx, .tsx, .mjs, .cjs)
bunx --bun eslint <files> >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null
bunx --bun tsc --noEmit >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null
true

# JSON (.json)
for f in <json-files>; do
  python3 -m json.tool "$f" > /dev/null 2>&1 || echo "error: JSON INVALID: $f" >> "$REPORT_DIR/pass0-lint-raw.txt"
done

# YAML (.yaml, .yml)
uvx yamllint -d relaxed <files> >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null || true

# TOML (.toml)
for f in <toml-files>; do
  python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "$f" 2>&1 || echo "error: TOML INVALID: $f"
done >> "$REPORT_DIR/pass0-lint-raw.txt"

# XML/SVG (.xml, .svg, .xhtml)
for f in <xml-files>; do
  python3 -c "import xml.etree.ElementTree as ET,sys; ET.parse(sys.argv[1])" "$f" 2>&1 || echo "error: XML INVALID: $f"
done >> "$REPORT_DIR/pass0-lint-raw.txt"

# HTML (.html, .htm)
for f in <html-files>; do
  python3 -c "
import sys, html.parser
class P(html.parser.HTMLParser):
    def handle_starttag(s,t,a): pass
p = P()
p.feed(open(sys.argv[1]).read())
" "$f" 2>&1 || echo "error: HTML INVALID: $f"
done >> "$REPORT_DIR/pass0-lint-raw.txt"

# Shell (.sh, .bash, .zsh)
shellcheck <files> >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null; true

# CSS/SCSS/LESS (.css, .scss, .less)
bunx --bun stylelint <files> >> "$REPORT_DIR/pass0-lint-raw.txt" 2>/dev/null; true

# Rust (.rs) — only if Cargo.toml exists
cargo check 2>> "$REPORT_DIR/pass0-lint-raw.txt"; true

# Go (.go) — only if go.mod exists
go vet <files> 2>> "$REPORT_DIR/pass0-lint-raw.txt"; true
```

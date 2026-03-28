#!/usr/bin/env python3
"""triage.py — Recheck pipeline triage: detect files, lint, classify, output manifest.

Performs all mechanical work so the orchestrator only does dispatch:
  1. Recursion guard (checks for [rechecker: skip] in HEAD commit)
  2. Session setup (UUID, timestamps, report dir, token snapshot)
  3. File detection from HEAD commit (git show --name-only)
  4. File filtering (skip media, binary, generated, lock, >500KB)
  5. Size classification (normal ≤250KB vs large >250KB)
  6. Lint execution by file type (all linters, grouped by extension)
  7. Lint error filtering (errors only — no haiku agent needed)
  8. Security pass detection (do any files touch auth/network/crypto?)
  9. Group splitting — writes per-group JSON files for parallel dispatch

Output: compact JSON manifest to stdout (no file paths inline — only
paths to group JSON files). Each group gets a short ID so reports and
group files stay correlated through the entire pipeline.

Usage:
    python3 triage.py [--plugin-root <path>] [--max-group-size N]

Exit codes:
    0 = manifest printed, proceed with review
    3 = skip (recursion guard triggered or no reviewable files)
    1 = error
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# -- File classification -------------------------------------------------------

SKIP_EXTENSIONS = frozenset({
    # Media
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".mp3", ".mp4", ".webm",
    ".webp", ".avif", ".bmp", ".tiff", ".eps", ".ai", ".pdf",
    # Binary
    ".whl", ".egg", ".so", ".dylib", ".dll", ".exe", ".bin", ".o",
    ".a", ".class", ".pyc", ".pyo",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Data blobs
    ".sqlite", ".db", ".parquet", ".csv", ".tsv",
    # Generated
    ".min.js", ".min.css", ".map", ".bundle.js", ".chunk.js",
    # Lock files
    ".lock", ".lockb",
})

SKIP_BASENAMES = frozenset({
    "CHANGELOG.md", "LICENSE", "LICENSE.md", "LICENSE.txt",
    "package-lock.json", "yarn.lock", "Cargo.lock", "uv.lock",
})

# Extensions that suggest security-relevant code
SECURITY_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".cs",
    ".php", ".swift", ".kt",
})

# Patterns in filenames/paths that suggest security relevance
SECURITY_PATTERNS = re.compile(
    r"auth|login|session|token|password|secret|crypt|oauth|jwt|"
    r"middleware|route|handler|endpoint|api|fetch|request|"
    r"upload|deserializ|serial|pickle|yaml\.load|"
    r"subprocess|shell|exec|sql|query|db|database|"
    r"prompt|inject",
    re.IGNORECASE,
)

MAX_FILE_SIZE = 500 * 1024         # 500KB — skip files larger than this
LARGE_FILE_THRESHOLD = 250 * 1024  # 250KB — opus agent instead of LLM Externalizer
DEFAULT_MAX_GROUP_SIZE = 10        # max files per review group

# Linter extension groups
LINTER_GROUPS: dict[str, list[str]] = {
    "python": [".py"],
    "javascript": [".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"],
    "json": [".json"],
    "yaml": [".yaml", ".yml"],
    "toml": [".toml"],
    "xml": [".xml", ".svg", ".xhtml"],
    "html": [".html", ".htm"],
    "shell": [".sh", ".bash", ".zsh"],
    "css": [".css", ".scss", ".less"],
    "rust": [".rs"],
    "go": [".go"],
}

# -- Helpers -------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, return result. Never raises."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="")


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _short_id(index: int) -> str:
    """Generate a short group ID like g01, g02, etc."""
    return f"g{index:02d}"


# -- Core steps ----------------------------------------------------------------


def check_recursion_guard() -> bool:
    """Return True if we should SKIP (commit is already a rechecker commit)."""
    r = _run(["git", "log", "-1", "--format=%s"])
    return "[rechecker: skip]" in r.stdout


def get_changed_files() -> list[str]:
    """Get files changed in HEAD commit (deleted files excluded)."""
    r = _run(["git", "show", "--name-only", "--format=", "--diff-filter=d", "HEAD"])
    return [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]


def classify_file(path: str) -> dict | None:
    """Classify a file. Returns dict with metadata or None if skipped."""
    p = Path(path)
    basename = p.name
    ext = p.suffix.lower()

    # Skip by basename
    if basename in SKIP_BASENAMES:
        return None

    # Skip by extension (handle compound like .tar.gz, .min.js)
    if ext in SKIP_EXTENSIONS:
        return None
    if basename.endswith((".tar.gz", ".min.js", ".min.css", ".bundle.js", ".chunk.js")):
        return None

    # Skip if file doesn't exist (deleted in worktree but in commit)
    if not p.is_file():
        return None

    # Skip by size
    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        return None

    # Determine if security-relevant
    security_relevant = False
    if ext in SECURITY_EXTENSIONS:
        if SECURITY_PATTERNS.search(path):
            security_relevant = True
        else:
            # Quick content scan for security keywords (first 4KB only)
            try:
                with open(p, "r", errors="ignore") as f:
                    head = f.read(4096)
                if SECURITY_PATTERNS.search(head):
                    security_relevant = True
            except OSError:
                pass

    return {
        "path": path,
        "abs_path": str(p.resolve()),
        "extension": ext,
        "size": size,
        "category": "large" if size > LARGE_FILE_THRESHOLD else "normal",
        "security_relevant": security_relevant,
    }


def group_files_by_extension(files: list[dict]) -> dict[str, list[str]]:
    """Group file paths by linter category."""
    groups: dict[str, list[str]] = {}
    ext_to_group: dict[str, str] = {}
    for group_name, exts in LINTER_GROUPS.items():
        for ext in exts:
            ext_to_group[ext] = group_name

    for f in files:
        group = ext_to_group.get(f["extension"])
        if group:
            groups.setdefault(group, []).append(f["path"])
    return groups


def run_linters(lint_groups: dict[str, list[str]], report_dir: Path) -> str:
    """Run linters for each group, write raw output. Returns path to raw lint file."""
    raw_file = report_dir / "pass0-lint-raw.txt"
    output_lines: list[str] = []

    for group, files in sorted(lint_groups.items()):
        if group == "python":
            if _has_tool("uvx"):
                r = _run(["uvx", "ruff", "check"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)
                r = _run(["uvx", "mypy"] + files + ["--ignore-missing-imports"])
                if r.stdout.strip():
                    output_lines.append(r.stdout)

        elif group == "javascript":
            # Try bunx first (fast, bun-native), fall back to npx (node)
            if _has_tool("bunx"):
                r = _run(["bunx", "--bun", "eslint"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)
                r = _run(["bunx", "--bun", "tsc", "--noEmit"])
                if r.stdout.strip():
                    output_lines.append(r.stdout)
            elif _has_tool("npx"):
                r = _run(["npx", "--yes", "eslint"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)
                r = _run(["npx", "--yes", "tsc", "--noEmit"])
                if r.stdout.strip():
                    output_lines.append(r.stdout)

        elif group == "json":
            for f in files:
                r = _run(["python3", "-m", "json.tool", f])
                if r.returncode != 0:
                    output_lines.append(f"error: JSON INVALID: {f}")

        elif group == "yaml":
            if _has_tool("uvx"):
                r = _run(["uvx", "yamllint", "-d", "relaxed"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)

        elif group == "toml":
            for f in files:
                r = _run([
                    "python3", "-c",
                    "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))",
                    f,
                ])
                if r.returncode != 0:
                    output_lines.append(f"error: TOML INVALID: {f}")

        elif group == "xml":
            for f in files:
                r = _run([
                    "python3", "-c",
                    "import xml.etree.ElementTree as ET,sys; ET.parse(sys.argv[1])",
                    f,
                ])
                if r.returncode != 0:
                    output_lines.append(f"error: XML INVALID: {f}")

        elif group == "html":
            for f in files:
                r = _run([
                    "python3", "-c",
                    "import sys,html.parser\n"
                    "class P(html.parser.HTMLParser):\n"
                    " def handle_starttag(s,t,a):pass\n"
                    "P().feed(open(sys.argv[1]).read())",
                    f,
                ])
                if r.returncode != 0:
                    output_lines.append(f"error: HTML INVALID: {f}")

        elif group == "shell":
            if _has_tool("shellcheck"):
                r = _run(["shellcheck"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)

        elif group == "css":
            if _has_tool("bunx"):
                r = _run(["bunx", "--bun", "stylelint"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)
            elif _has_tool("npx"):
                r = _run(["npx", "--yes", "stylelint"] + files)
                if r.stdout.strip():
                    output_lines.append(r.stdout)

        elif group == "rust":
            if _has_tool("cargo") and Path("Cargo.toml").is_file():
                r = _run(["cargo", "check"])
                if r.stderr.strip():
                    output_lines.append(r.stderr)

        elif group == "go":
            if _has_tool("go") and Path("go.mod").is_file():
                r = _run(["go", "vet"] + files)
                if r.stderr.strip():
                    output_lines.append(r.stderr)

    raw_text = "\n".join(output_lines)
    raw_file.write_text(raw_text)
    return str(raw_file)


def filter_lint_errors(raw_file: Path) -> tuple[str, list[str]]:
    """Filter lint output to errors only. Returns (output_path, error_lines)."""
    errors_file = raw_file.parent / "pass0-lint-errors.txt"
    raw_text = raw_file.read_text()
    if not raw_text.strip():
        errors_file.write_text("NO ERRORS")
        return str(errors_file), []

    error_lines: list[str] = []
    for line in raw_text.splitlines():
        line_lower = line.lower()
        # Always keep INVALID lines (JSON/TOML/XML/HTML validators)
        if "invalid:" in line_lower:
            error_lines.append(line)
            continue
        # Skip warnings/notes/info
        if any(w in line_lower for w in [": warning:", ": note:", ": info:", "warning ", "(w"]):
            continue
        # Keep errors — ruff codes start with E/F followed by digits like (E501), (F401)
        if ": error:" in line_lower or ": error " in line_lower:
            error_lines.append(line)
        elif re.search(r"\([EF]\d{3}", line):
            error_lines.append(line)

    if not error_lines:
        errors_file.write_text("NO ERRORS")
        return str(errors_file), []

    errors_file.write_text("\n".join(error_lines))
    return str(errors_file), error_lines


# -- Group splitting -----------------------------------------------------------


def split_into_groups(
    files: list[dict],
    report_dir: Path,
    max_group_size: int,
) -> list[dict]:
    """Split classified files into review groups and write per-group JSON files.

    Each group gets a short ID (g01, g02, ...). The group JSON file is written
    to report_dir/group-<id>.json and contains only the files for that group.
    The orchestrator never sees individual file paths — only group file paths.

    Grouping strategy:
    - Large files get one group each (opus agent, 1 file per group)
    - Normal files are batched by extension family up to max_group_size
    - If a batch exceeds max_group_size, it's split into sub-groups

    Returns list of group descriptors (no file paths, only group metadata).
    """
    groups: list[dict] = []
    group_index = 0

    # 1. Large files: one group per file (each needs its own opus agent)
    large_files = [f for f in files if f["category"] == "large"]
    for lf in large_files:
        gid = _short_id(group_index)
        group_index += 1
        group_data = {
            "group_id": gid,
            "category": "large",
            "file_count": 1,
            "files": [lf],
        }
        group_path = report_dir / f"group-{gid}.json"
        group_path.write_text(json.dumps(group_data, indent=2))
        groups.append({
            "group_id": gid,
            "category": "large",
            "review_with": "opus",
            "file_count": 1,
            "group_file": str(group_path),
            "report_file": str(report_dir / f"review-{gid}.md"),
            "fixes_file": str(report_dir / f"fixes-{gid}.md"),
            "security_relevant": lf["security_relevant"],
        })

    # 2. Normal files: batch by extension family
    normal_files = [f for f in files if f["category"] == "normal"]

    # Group by linter family first for coherent review context
    ext_to_family: dict[str, str] = {}
    for family, exts in LINTER_GROUPS.items():
        for ext in exts:
            ext_to_family[ext] = family
    # Files not matching any linter family go to "other"
    by_family: dict[str, list[dict]] = {}
    for nf in normal_files:
        family = ext_to_family.get(nf["extension"], "other")
        by_family.setdefault(family, []).append(nf)

    for family, family_files in sorted(by_family.items()):
        # Split into chunks of max_group_size
        for chunk_start in range(0, len(family_files), max_group_size):
            chunk = family_files[chunk_start:chunk_start + max_group_size]
            gid = _short_id(group_index)
            group_index += 1
            group_data = {
                "group_id": gid,
                "category": "normal",
                "family": family,
                "file_count": len(chunk),
                "files": chunk,
            }
            group_path = report_dir / f"group-{gid}.json"
            group_path.write_text(json.dumps(group_data, indent=2))
            any_security = any(cf["security_relevant"] for cf in chunk)
            groups.append({
                "group_id": gid,
                "category": "normal",
                "review_with": "llm_externalizer",
                "family": family,
                "file_count": len(chunk),
                "group_file": str(group_path),
                "report_file": str(report_dir / f"review-{gid}.md"),
                "fixes_file": str(report_dir / f"fixes-{gid}.md"),
                "security_relevant": any_security,
            })

    return groups


# -- Lint error grouping -------------------------------------------------------


def split_lint_errors_by_group(
    error_lines: list[str],
    groups: list[dict],
    report_dir: Path,
) -> dict[str, str]:
    """Split lint errors into per-group error files.

    Matches each error line to a group by checking if the file path in the
    error line belongs to any file in the group. Returns {group_id: error_file_path}.
    """
    # Build a map: path -> group_id (re-reads the small group JSONs we just wrote)
    file_to_group: dict[str, str] = {}
    for g in groups:
        group_data = json.loads(Path(g["group_file"]).read_text())
        for f in group_data["files"]:
            file_to_group[f["abs_path"]] = g["group_id"]
            file_to_group[f["path"]] = g["group_id"]

    # Assign each error line to a group
    group_errors: dict[str, list[str]] = {}
    for line in error_lines:
        # Error lines typically start with the file path before the first ":"
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        candidate = parts[0].strip()
        # Try matching against known files
        matched_gid = file_to_group.get(candidate)
        if not matched_gid:
            # Try resolving the path
            try:
                resolved = str(Path(candidate).resolve())
                matched_gid = file_to_group.get(resolved)
            except (OSError, ValueError):
                pass
        if matched_gid:
            group_errors.setdefault(matched_gid, []).append(line)

    # Write per-group error files
    result: dict[str, str] = {}
    for gid, lines in group_errors.items():
        err_file = report_dir / f"lint-errors-{gid}.txt"
        err_file.write_text("\n".join(lines))
        result[gid] = str(err_file)

    return result


# -- Main ----------------------------------------------------------------------


def main() -> int:
    # Parse args
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    max_group_size = DEFAULT_MAX_GROUP_SIZE
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    i = 0
    while i < len(args):
        if args[i] == "--plugin-root" and i + 1 < len(args):
            plugin_root = args[i + 1]
            i += 2
        elif args[i] == "--max-group-size" and i + 1 < len(args):
            try:
                max_group_size = int(args[i + 1])
            except ValueError:
                print(f"error: --max-group-size must be an integer, got: {args[i + 1]}", file=sys.stderr)
                return 1
            if max_group_size < 1:
                print("error: --max-group-size must be >= 1", file=sys.stderr)
                return 1
            i += 2
        else:
            i += 1

    # Step 1: Recursion guard
    if check_recursion_guard():
        print(json.dumps({"status": "skip", "reason": "recursion guard"}))
        return 3

    # Step 2: Detect and classify files
    changed = get_changed_files()
    files = []
    for path in changed:
        info = classify_file(path)
        if info:
            files.append(info)

    if not files:
        print(json.dumps({"status": "skip", "reason": "no reviewable files"}))
        return 3

    # Step 3: Session setup
    rck_uuid = uuid.uuid4().hex[:12]
    rck_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rck_commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    rck_commit_short = rck_commit[:7]
    report_dir = Path("reports_dev") / f"rck-{rck_uuid}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # Step 4: Token snapshot
    # Resolve plugin root: env var > --plugin-root arg > infer from __file__
    if not plugin_root:
        plugin_root = str(Path(__file__).resolve().parent.parent)
    snapshot_path = report_dir / "before-tokens.json"
    count_script = Path(plugin_root) / "scripts" / "count-tokens.py"
    if count_script.is_file():
        _run([sys.executable, str(count_script), "--snapshot", str(snapshot_path)])

    # Step 5: Split into groups and write per-group JSON files
    groups = split_into_groups(files, report_dir, max_group_size)

    # Step 6: Lint
    lint_groups = group_files_by_extension(files)
    raw_lint_path = run_linters(lint_groups, report_dir)
    errors_path, error_lines = filter_lint_errors(Path(raw_lint_path))

    # Step 7: Split lint errors into per-group files
    lint_by_group = split_lint_errors_by_group(error_lines, groups, report_dir)

    # Step 8: Security pass detection
    needs_security = any(g["security_relevant"] for g in groups)

    # Step 9: Build compact manifest — no file paths, only group file paths
    manifest = {
        "status": "proceed",
        "session": {
            "uuid": rck_uuid,
            "commit": rck_commit,
            "commit_short": rck_commit_short,
            "started": rck_start,
            "report_dir": str(report_dir),
            "snapshot_path": str(snapshot_path),
            "plugin_root": plugin_root,
        },
        "files_total": len(files),
        "groups": [
            {
                "id": g["group_id"],
                "category": g["category"],
                "review_with": g["review_with"],
                "file_count": g["file_count"],
                "group_file": g["group_file"],
                "report_file": g["report_file"],
                "fixes_file": g["fixes_file"],
                "lint_errors_file": lint_by_group.get(g["group_id"]),
                "security_relevant": g["security_relevant"],
            }
            for g in groups
        ],
        "lint": {
            "raw_file": raw_lint_path,
            "errors_file": errors_path,
            "error_count": len(error_lines),
            "has_errors": len(error_lines) > 0,
        },
        "security_pass": needs_security,
    }

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

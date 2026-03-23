#!/usr/bin/env python3
"""pipeline.py - Rechecker pipeline helper.

CLI tool called by the orchestrator agent to manage file grouping,
report merging, and issue counting across the 4-loop pipeline.

Naming convention:
  rck-{YYYYMMDD_HHMMSS}_{UID}-[LP00002-IT00006-FID00999]-{purpose}.{ext}

Tag levels:
  [LP00002-IT00006-FID00999]  file-level (fix report, review report)
  [LP00002-IT00006]           iteration-level (merged fix reports)
  [LP00002]                   loop-level (merged iteration reports)
  (no tag)                    final report

Subcommands:
  init            Create index from files.txt, assign FIDs, create groups
  groups          List groups and their files (JSON to stdout)
  merge-iteration Merge fix reports into iteration report
  merge-loop      Merge iteration reports into loop report
  merge-final     Merge loop reports into final report + cleanup
  count-issues    Count issues in review reports for a loop/iteration
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

RECHECKER_DIR = Path(".rechecker")
REPORTS_DIR = RECHECKER_DIR / "reports"
INDEX_FILE = RECHECKER_DIR / "index.json"
PROGRESS_FILE = RECHECKER_DIR / "rck-progress.json"

# File size thresholds for grouping (big = one per agent group, small = batched)
BIG_FILE_LINES = 200
BIG_FILE_BYTES = 10_000

# Files above this threshold are routed to big-files-auditor (opus single-pass)
# instead of the normal LLM Externalizer review loop.
# 100KB ≈ 25K tokens — conservative limit for externalizer reliability.
HUGE_FILE_BYTES = 100_000

# Files above this limit are skipped entirely — too large even for opus[1m].
# 500KB ≈ 125K tokens. Leaves room for system prompt + output in 1M context.
MAX_FILE_BYTES = 500_000

# Group size limits
MAX_BIG_PER_GROUP = 3
PREFER_BIG_PER_GROUP = 1
MAX_SMALL_PER_GROUP = 10
PREFER_SMALL_PER_GROUP = 7

# Agent limits
MAX_AGENTS = 20
MAX_FILES_PER_MACRO = MAX_AGENTS * MAX_SMALL_PER_GROUP  # 200


# ── File filter: blocklist approach ──────────────────────────────────────
# Instead of enumerating every script/config/UI extension (hundreds), we SKIP
# known non-code files and INCLUDE everything else. This catches all source
# code, scripts (.ahk, .au3, .coffee, .ps1, ...), config, build files,
# UI/layout (.xib, .storyboard, .xaml, .axaml, .qml, .ui, .razor, .cshtml,
# .aspx, .lottie, .fxml, .kv, .glade, .nib, ...), and agent/skill/command
# definitions automatically without maintaining a massive allowlist.

# Filenames to always SKIP (case-insensitive)
_SKIP_FILENAMES = {
    # Documentation / meta (not code)
    "changelog.md", "changes.md", "history.md",
    "backlog.md", "todo.md", "notes.md",
    "release_notes.md", "release-notes.md", "releases.md",
    "contributing.md", "contributors.md", "code_of_conduct.md",
    "authors.md", "authors", "credits.md",
    "license", "license.md", "license.txt", "licence", "licence.md",
    "security.md", "funding.yml", "citation.cff",
    # Lock files (auto-generated)
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "composer.lock", "cargo.lock", "gemfile.lock", "poetry.lock",
    "uv.lock", "pdm.lock", "pipdeptree.json", "shrinkwrap.json",
    "packages.lock.json", "project.assets.json",
    # Other auto-generated
    ".ds_store", "thumbs.db", "desktop.ini",
}

# Extensions to always SKIP (blocklist — binary, media, docs, generated)
_SKIP_EXTENSIONS = {
    # Documentation markup (non-code)
    ".mdx", ".rst", ".adoc", ".textile",
    # Logs
    ".log",
    # Lock / generated
    ".lock", ".lockb",
    # Minified / compiled JS/CSS (not worth reviewing)
    ".min.js", ".min.css", ".map",
    # TypeScript declarations (auto-generated)
    ".d.ts",
    # Test snapshots / fixtures
    ".snap", ".fixture",
    # Patches
    ".patch", ".diff",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".avif",
    ".tiff", ".tif", ".psd", ".ai", ".eps", ".raw", ".cr2", ".nef",
    ".heic", ".heif", ".jxl",
    # Video
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv",
    ".m4v", ".3gp", ".ogv",
    # Audio
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".wma", ".m4a", ".opus",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Archives / packages
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst",
    ".tgz", ".tbz2", ".txz", ".jar", ".war", ".ear",
    ".deb", ".rpm", ".dmg", ".msi", ".pkg", ".apk", ".ipa",
    ".appimage", ".flatpak", ".snap",
    # Office documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
    # Compiled / binary objects
    ".pyc", ".pyo", ".class", ".o", ".obj", ".a", ".lib",
    ".so", ".dylib", ".dll", ".exe", ".com", ".bin",
    ".nbin", ".co", ".rpyc", ".elc", ".beam",
    ".wasm", ".bc",
    # Compiled platform binaries
    ".baml", ".xap", ".xbap", ".wmlsc", ".scptd",
    # Database
    ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    # Disk / VM images
    ".iso", ".img", ".vmdk", ".vdi", ".qcow2",
    # Certificates / keys (sensitive, don't review)
    ".pem", ".crt", ".cer", ".key", ".p12", ".pfx", ".jks",
    # Data dumps (too large, not code)
    ".sql", ".csv", ".tsv", ".parquet", ".arrow", ".feather",
    ".npy", ".npz", ".h5", ".hdf5",
    # Model files
    ".onnx", ".pt", ".pth", ".safetensors", ".gguf", ".ggml",
    ".pb", ".tflite",
    # Figma (binary)
    ".fig",
}

# Filenames to always INCLUDE (case-insensitive, overrides skip rules)
_RECHECK_FILENAMES = {
    "readme.md",
    # Build / task files without standard extensions
    "makefile", "gnumakefile", "bsdmakefile", "cmakelists.txt",
    "dockerfile", "containerfile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "justfile", "taskfile.yml", "taskfile.yaml", "rakefile", "gemfile",
    "gruntfile.js", "gulpfile.js", "webpack.config.js", "rollup.config.js",
    "vite.config.ts", "vite.config.js", "next.config.js", "next.config.mjs",
    "tsconfig.json", "jsconfig.json", "babel.config.js", "babel.config.json",
    ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
    ".prettierrc", ".prettierrc.js", ".prettierrc.json",
    "tox.ini", "setup.cfg", "setup.py", "pyproject.toml",
    "cargo.toml", "go.mod", "go.sum",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "pom.xml", "build.xml", "build.sbt",
    "package.json", "deno.json", "deno.jsonc",
    # Claude Code agent/skill definitions
    "skill.md",
}

# Path patterns that identify agent/skill/command/rules markdown files
_RECHECK_PATH_PATTERNS = [
    "agents/", "skills/", "commands/", "rules/",
]


def _should_recheck(file_path: str) -> bool:
    """Blocklist filter: skip known non-code files, include everything else.

    This catches all source code, scripts, config, build, UI/layout files
    automatically without maintaining a massive allowlist.
    """
    p = Path(file_path)
    name_lower = p.name.lower()
    suffix_lower = p.suffix.lower()
    # Compound extensions like .min.js, .d.ts
    suffixes_lower = "".join(s.lower() for s in p.suffixes)

    # 1. Always skip known non-code filenames
    if name_lower in _SKIP_FILENAMES:
        return False

    # 2. Always include known build/config filenames
    if name_lower in _RECHECK_FILENAMES:
        return True

    # 3. Skip by extension (binary, media, docs, generated)
    if suffix_lower in _SKIP_EXTENSIONS:
        return False
    if suffixes_lower in _SKIP_EXTENSIONS:
        return False

    # 4. .md files: only include agent/skill/command/rules, skip other docs
    path_str = file_path.replace("\\", "/").lower()
    if suffix_lower == ".md":
        return any(pat in path_str for pat in _RECHECK_PATH_PATTERNS)

    # 5. Files with no extension: include only known build tool names
    if not suffix_lower:
        return name_lower in _RECHECK_FILENAMES

    # 6. Everything else with a text-based extension → INCLUDE
    # This catches all source code, scripts, config, UI/layout, etc.
    return True


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(errors="replace").splitlines())
    except OSError:
        return 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_index() -> dict:  # type: ignore[type-arg]
    if not INDEX_FILE.exists():
        print("ERROR: .rechecker/index.json not found. Run 'init' first.", file=sys.stderr)
        sys.exit(1)
    result: dict = json.loads(INDEX_FILE.read_text())  # type: ignore[type-arg]
    return result


def _save_index(index: dict) -> None:
    INDEX_FILE.write_text(json.dumps(index, indent=2) + "\n")


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: write to temp file in same dir, then os.rename()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".rck-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_progress() -> dict:  # type: ignore[type-arg]
    if not PROGRESS_FILE.exists():
        return {}
    result: dict = json.loads(PROGRESS_FILE.read_text())  # type: ignore[type-arg]
    return result


def _save_progress(progress: dict) -> None:
    progress["updated"] = datetime.now().isoformat()
    _atomic_write_json(PROGRESS_FILE, progress)


# Strict tag validation — the bracket content must match EXACTLY one of 3 forms.
# The regex captures the full bracket group and validates its internal structure.
# Any other combination (wrong order, missing middle, extra fields, wrong digit count) is rejected.
#
# Valid:
#   [LP00001-IT00001-FID00001]  → file-level (all 3, correct order)
#   [LP00001-IT00001]           → iteration-level (LP + IT only)
#   [LP00001]                   → loop-level (LP only)
#
# Invalid (examples):
#   [LP00002-FID00011]          → missing IT (can't skip middle)
#   [IT00001-FID00120]          → missing LP (must start with LP)
#   [FID00002-LP00003]          → wrong order
#   [IT00001]                   → missing LP
#   [FID00011]                  → missing LP and IT
#   [IT00010-LP00001-FID00010]  → wrong order
#   [LP00002-IT00001-FID00]     → FID has wrong digit count
#   [LP00002-IT00001-FID000001] → FID has wrong digit count (6 digits)
#
# The regex extracts the bracket content and validates it as a whole string,
# not as a substring search — preventing partial matches inside longer tags.
_RE_BRACKET_TAG = re.compile(r"\[([^\]]+)\]")
_VALID_FILE = re.compile(r"^LP\d{5}-IT\d{5}-FID\d{5}$")
_VALID_ITER = re.compile(r"^LP\d{5}-IT\d{5}$")
_VALID_LOOP = re.compile(r"^LP\d{5}$")


def classify_report(filename: str) -> str | None:
    """Classify a report filename by its bracket tag. Returns 'file', 'iteration', 'loop', or None.

    Validates the ENTIRE bracket content against exactly 3 valid forms.
    Any malformed, partial, wrong-order, or wrong-digit-count tag returns None.
    """
    m = _RE_BRACKET_TAG.search(filename)
    if not m:
        return None
    tag_content = m.group(1)
    if _VALID_FILE.match(tag_content):
        return "file"
    if _VALID_ITER.match(tag_content):
        return "iteration"
    if _VALID_LOOP.match(tag_content):
        return "loop"
    return None


def _extract_fid(filename: str) -> str | None:
    """Extract FIDxxxxx from a validated file-level tag only. Returns None for any other tag type."""
    if classify_report(filename) != "file":
        return None
    m = _RE_BRACKET_TAG.search(filename)
    if not m:
        return None
    tag_content = m.group(1)
    fid_match = re.search(r"FID(\d{5})$", tag_content)
    return f"FID{fid_match.group(1)}" if fid_match else None


def _find_reports(directory: Path, loop: str, iter_: str | None, fid: str | None, suffix: str) -> list[Path]:
    """Find report files by loop/iter/fid/suffix. Strict tag validation — partial tags are rejected."""
    lp = f"LP{int(loop):05d}"
    if fid is not None and iter_ is not None:
        # File-level: all 3 groups required
        it = f"IT{int(iter_):05d}"
        tag_pattern = re.compile(re.escape(f"[{lp}-{it}-{fid}]"))
        expected_level = "file"
    elif iter_ is not None:
        # Search for file-level reports within a specific iteration (any FID)
        it = f"IT{int(iter_):05d}"
        tag_pattern = re.compile(re.escape(f"[{lp}-{it}-") + r"FID\d{5}\]")
        expected_level = "file"
    else:
        # Infer expected level from the suffix
        if "iteration" in suffix:
            tag_pattern = re.compile(re.escape(f"[{lp}-") + r"IT\d{5}\]")
            expected_level = "iteration"
        elif "loop" in suffix:
            tag_pattern = re.compile(re.escape(f"[{lp}]"))
            expected_level = "loop"
        else:
            tag_pattern = re.compile(re.escape(f"[{lp}") + r"[-\]]")
            expected_level = None
    full_pattern = re.compile(r".*" + tag_pattern.pattern + r"-" + re.escape(suffix))

    results = []
    if directory.is_dir():
        for f in directory.iterdir():
            if not full_pattern.match(f.name):
                continue
            # Validate tag level — reject partial/malformed tags
            level = classify_report(f.name)
            if expected_level is not None and level != expected_level:
                continue
            if level is None:
                continue  # no valid tag at all
            results.append(f)
    return sorted(results)


def _group_files(fids_with_info: list[tuple[str, dict]]) -> dict[str, list[str]]:
    """Group files into agent-sized batches. Returns {group_id: [fid, ...]}."""
    big = [(fid, info) for fid, info in fids_with_info if info["category"] == "big"]
    small = [(fid, info) for fid, info in fids_with_info if info["category"] == "small"]

    groups: dict[str, list[str]] = {}
    gid = 0

    # Big files: prefer 1 per group, allow up to 3
    # First calculate how many group slots small files need
    small_groups_needed = (len(small) + PREFER_SMALL_PER_GROUP - 1) // PREFER_SMALL_PER_GROUP if small else 0
    big_slots = max(1, MAX_AGENTS - small_groups_needed)
    # How many big files per group to fit within big_slots?
    big_per_group = max(PREFER_BIG_PER_GROUP, (len(big) + big_slots - 1) // big_slots) if big else 1
    big_per_group = min(big_per_group, MAX_BIG_PER_GROUP)

    i = 0
    while i < len(big):
        gid += 1
        take = min(big_per_group, len(big) - i)
        groups[f"G{gid:03d}"] = [fid for fid, _ in big[i:i + take]]
        i += take

    # Small files: prefer 7, allow up to 10
    i = 0
    while i < len(small):
        gid += 1
        remaining = len(small) - i
        current_groups = len(groups)
        # If we'd exceed MAX_AGENTS, pack more per group
        remaining_groups_needed = (remaining + PREFER_SMALL_PER_GROUP - 1) // PREFER_SMALL_PER_GROUP
        if current_groups + remaining_groups_needed > MAX_AGENTS:
            take = min(MAX_SMALL_PER_GROUP, remaining)
        else:
            take = min(PREFER_SMALL_PER_GROUP, remaining)
        groups[f"G{gid:03d}"] = [fid for fid, _ in small[i:i + take]]
        i += take

    return groups


# ── Subcommands ──────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> None:
    """Create index from files.txt, assign FIDs, create groups."""
    files_txt = RECHECKER_DIR / "files.txt"
    if not files_txt.exists():
        print("ERROR: .rechecker/files.txt not found", file=sys.stderr)
        sys.exit(1)

    all_paths = [line.strip() for line in files_txt.read_text().splitlines() if line.strip()]
    if not all_paths:
        print("ERROR: No files in .rechecker/files.txt", file=sys.stderr)
        sys.exit(1)

    # Filter to only files worth rechecking (source code, config, agents, etc.)
    file_paths = [p for p in all_paths if _should_recheck(p)]
    skipped = len(all_paths) - len(file_paths)
    if skipped:
        print(f"Filtered: {len(file_paths)} files to recheck, {skipped} skipped (docs, logs, media, etc.)")
    if not file_paths:
        print("No recheckable files in commit — all skipped.")
        sys.exit(0)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Assign FIDs and measure sizes
    files: dict[str, dict] = {}
    skipped_too_large: list[str] = []
    for i, path_str in enumerate(file_paths, 1):
        fid = f"FID{i:05d}"
        p = Path(path_str)
        lines = _count_lines(p)
        size = _file_size(p)
        # Files above MAX_FILE_BYTES are skipped entirely — too large even for opus[1m]
        if size > MAX_FILE_BYTES:
            skipped_too_large.append(f"{path_str} ({size // 1024}KB)")
            continue
        huge = size > HUGE_FILE_BYTES
        category = "huge" if huge else ("big" if lines > BIG_FILE_LINES or size > BIG_FILE_BYTES else "small")
        files[fid] = {
            "path": path_str,
            "lines": lines,
            "bytes": size,
            "category": category,
        }
    if skipped_too_large:
        print(f"Skipped {len(skipped_too_large)} file(s) exceeding {MAX_FILE_BYTES // 1024}KB limit:")
        for s in skipped_too_large:
            print(f"  {s}")

    # Huge files go to big-files-auditor, not through the normal pipeline groups
    huge_fids = [fid for fid, info in files.items() if info["category"] == "huge"]
    # Only group non-huge files
    fid_list = [fid for fid in files if files[fid]["category"] != "huge"]
    macro_groups: dict[str, list[str]] = {}  # macro_id -> list of group IDs
    all_groups: dict[str, list[str]] = {}  # group_id -> list of FIDs

    if len(fid_list) <= MAX_FILES_PER_MACRO:
        # Single macro-group
        fids_with_info = [(fid, files[fid]) for fid in fid_list]
        groups = _group_files(fids_with_info)
        all_groups.update(groups)
        macro_groups["MG001"] = list(groups.keys())
    else:
        # Split files into chunks of MAX_FILES_PER_MACRO
        mg_id = 0
        for chunk_start in range(0, len(fid_list), MAX_FILES_PER_MACRO):
            mg_id += 1
            chunk_fids = fid_list[chunk_start:chunk_start + MAX_FILES_PER_MACRO]
            fids_with_info = [(fid, files[fid]) for fid in chunk_fids]
            groups = _group_files(fids_with_info)
            # Renumber groups to avoid collisions across macro-groups
            renumbered: dict[str, list[str]] = {}
            base = len(all_groups)
            for j, (_, fids) in enumerate(groups.items(), base + 1):
                renumbered[f"G{j:03d}"] = fids
            all_groups.update(renumbered)
            macro_groups[f"MG{mg_id:03d}"] = list(renumbered.keys())

    index = {
        "uid": args.uid,
        "created": datetime.now().isoformat(),
        "total_files": len(file_paths),
        "files": files,
        "groups": all_groups,
        "macro_groups": macro_groups,
        "huge_fids": huge_fids,
    }
    _save_index(index)

    # Print summary
    huge_count = len(huge_fids)
    big_count = sum(1 for f in files.values() if f["category"] == "big")
    small_count = sum(1 for f in files.values() if f["category"] == "small")
    print(f"Initialized: {len(files)} files, {len(all_groups)} groups, {len(macro_groups)} macro-group(s)")
    print(f"  Huge (>100KB, BFA): {huge_count}, Big: {big_count}, Small: {small_count}")
    if huge_fids:
        for fid in huge_fids:
            info = files[fid]
            print(f"    {fid}: {info['path']} ({info['lines']} lines) -> big-files-auditor")
    for mg_key, mg_gids in macro_groups.items():
        file_count = sum(len(all_groups[g]) for g in mg_gids)
        print(f"  {mg_key}: {len(mg_gids)} groups, {file_count} files")


def cmd_groups(args: argparse.Namespace) -> None:
    """List groups and their files as JSON."""
    index = _load_index()
    if not index.get("macro_groups"):
        print("ERROR: No macro-groups in index. Run 'init' first.", file=sys.stderr)
        sys.exit(1)
    macro = args.macro or list(index["macro_groups"].keys())[0]

    if macro not in index["macro_groups"]:
        print(f"ERROR: Macro-group {macro} not found", file=sys.stderr)
        sys.exit(1)

    result = {}
    for gid in index["macro_groups"][macro]:
        fids = index["groups"][gid]
        result[gid] = [
            {"fid": fid, "path": index["files"][fid]["path"], "category": index["files"][fid]["category"]}
            for fid in fids
        ]

    print(json.dumps(result, indent=2))


def cmd_merge_iteration(args: argparse.Namespace) -> None:
    """Merge all fix reports for one iteration into a single iteration report."""
    index = _load_index()
    uid = index["uid"]
    lp = f"LP{int(args.loop):05d}"
    it = f"IT{int(args.iter):05d}"

    fix_reports = _find_reports(REPORTS_DIR, args.loop, args.iter, None, "fix.md")

    if not fix_reports:
        print(f"No fix reports for {lp}-{it}", file=sys.stderr)
        sys.exit(0)

    merged = [f"# Iteration Report: {lp} {it}", ""]

    for report in fix_reports:
        fid = _extract_fid(report.name)
        file_path = index["files"].get(fid, {}).get("path", "unknown") if fid else "unknown"
        merged.append(f"## {file_path} ({fid or '?'})")
        merged.append("")
        try:
            merged.append(report.read_text().strip())
        except OSError:
            merged.append("(unreadable)")
        merged.append("")
        merged.append("---")
        merged.append("")

    now = _now()
    out = REPORTS_DIR / f"rck-{now}_{uid}-[{lp}-{it}]-iteration.md"
    out.write_text("\n".join(merged) + "\n")
    print(str(out))


def cmd_merge_loop(args: argparse.Namespace) -> None:
    """Merge all iteration reports for one loop into a single loop report."""
    index = _load_index()
    uid = index["uid"]
    lp = f"LP{int(args.loop):05d}"

    iter_reports = _find_reports(REPORTS_DIR, args.loop, None, None, "iteration.md")

    if not iter_reports:
        print(f"No iteration reports for {lp}", file=sys.stderr)
        sys.exit(0)

    merged = [f"# Loop Report: {lp}", f"**Iterations**: {len(iter_reports)}", ""]

    for report in iter_reports:
        try:
            merged.append(report.read_text().strip())
        except OSError:
            merged.append(f"(unreadable: {report.name})")
        merged.append("")
        merged.append("===")
        merged.append("")

    now = _now()
    out = REPORTS_DIR / f"rck-{now}_{uid}-[{lp}]-loop.md"
    out.write_text("\n".join(merged) + "\n")
    print(str(out))


def cmd_merge_final(args: argparse.Namespace) -> None:
    """Merge all loop reports into final report. Cleanup intermediate files."""
    index = _load_index()
    uid = index["uid"]

    # Find all loop reports (any loop number)
    lp_pattern = re.compile(r".*\[LP\d{5}\]-loop\.md$")
    loop_reports = sorted(f for f in REPORTS_DIR.iterdir() if lp_pattern.match(f.name))

    if not loop_reports:
        print("No loop reports found", file=sys.stderr)
        sys.exit(0)

    merged = [
        "# Rechecker Final Report",
        "",
        f"**Date**: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"**UID**: {uid}",
        f"**Files checked**: {index['total_files']}",
        "",
    ]

    for report in loop_reports:
        try:
            merged.append(report.read_text().strip())
        except OSError:
            merged.append(f"(unreadable: {report.name})")
        merged.append("")

    now = _now()
    out_name = f"rck-{now}_{uid}-report.md"
    out = Path(out_name)  # worktree root
    out.write_text("\n".join(merged) + "\n")
    print(str(out))

    # Cleanup intermediate files unless --keep
    if not args.keep:
        for pattern in ["*-review.json", "*-review.md", "*-fix.md", "*-iteration.md", "*-loop.md"]:
            for f in REPORTS_DIR.glob(pattern):
                f.unlink()


def cmd_count_issues(args: argparse.Namespace) -> None:
    """Count total issues in review reports for a loop/iteration. Prints count. Exit 1 if >0.

    Supports both formats:
    - Markdown reviews (.md): counts ### BUG: and ### ISSUE: headers
    - JSON reviews (.json): counts array elements (legacy format)
    A report containing 'NO ISSUES FOUND' is treated as 0 issues.
    """
    total = 0

    # Check markdown review reports
    md_reports = _find_reports(REPORTS_DIR, args.loop, args.iter, None, "review.md")
    for report in md_reports:
        try:
            content = report.read_text()
            if "NO ISSUES FOUND" in content:
                continue
            total += content.count("### BUG:") + content.count("### ISSUE:")
        except OSError:
            pass

    # Also check JSON review reports (legacy/fallback)
    json_reports = _find_reports(REPORTS_DIR, args.loop, args.iter, None, "review.json")
    for report in json_reports:
        try:
            data = json.loads(report.read_text())
            if isinstance(data, list):
                total += len(data)
        except (json.JSONDecodeError, OSError):
            pass

    print(str(total))
    if total > 0:
        sys.exit(1)


# ── Progress tracking ────────────────────────────────────────────────────

# rck-progress.json schema:
# {
#   "uid": "ab1234",
#   "status": "running" | "completed" | "interrupted",
#   "created": "ISO datetime",
#   "updated": "ISO datetime",
#   "current_loop": 1,        # 1-4 (which loop is active)
#   "current_iter": 1,        # iteration within current loop
#   "loops": {
#     "1": {"status": "completed", "files_done": ["FID00001", ...]},
#     "2": {"status": "running", "iter": 3, "files_done": ["FID00001", ...],
#            "files_clean": ["FID00002", ...]},
#     "3": {"status": "pending"},
#     "4": {"status": "pending"}
#   },
#   "committed": false        # true after final git commit in Step 6
# }


def cmd_progress_init(_args: argparse.Namespace) -> None:
    """Initialize rck-progress.json at the start of a pipeline run."""
    index = _load_index()
    uid = index["uid"]

    progress = {
        "uid": uid,
        "status": "running",
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
        "current_loop": 1,
        "current_iter": 1,
        "loops": {
            "1": {"status": "pending", "files_done": [], "files_clean": []},
            "2": {"status": "pending", "iter": 0, "files_done": [], "files_clean": []},
            "3": {"status": "pending", "iter": 0, "files_done": [], "files_clean": []},
            "4": {"status": "pending", "files_done": [], "files_clean": []},
        },
        "committed": False,
    }
    _save_progress(progress)
    print(f"Progress initialized for uid={uid}")


def cmd_progress_update(args: argparse.Namespace) -> None:
    """Update progress after completing a step in the pipeline."""
    progress = _load_progress()
    if not progress:
        print("ERROR: rck-progress.json not found. Run 'progress-init' first.", file=sys.stderr)
        sys.exit(1)

    loop_key = str(args.loop)
    if loop_key not in progress["loops"]:
        print(f"ERROR: invalid loop {loop_key}", file=sys.stderr)
        sys.exit(1)

    loop_data = progress["loops"][loop_key]

    if args.action == "start-loop":
        loop_data["status"] = "running"
        progress["current_loop"] = int(args.loop)
        progress["current_iter"] = 1
        if "iter" in loop_data:
            loop_data["iter"] = 1

    elif args.action == "start-iter":
        progress["current_iter"] = int(args.iter)
        if "iter" in loop_data:
            loop_data["iter"] = int(args.iter)

    elif args.action == "file-done":
        if args.fid and args.fid not in loop_data["files_done"]:
            loop_data["files_done"].append(args.fid)

    elif args.action == "file-clean":
        if args.fid and args.fid not in loop_data["files_clean"]:
            loop_data["files_clean"].append(args.fid)

    elif args.action == "end-loop":
        loop_data["status"] = "completed"

    _save_progress(progress)
    print(f"Progress updated: loop={loop_key} action={args.action}")


def cmd_progress_complete(_args: argparse.Namespace) -> None:
    """Mark the entire pipeline as completed."""
    progress = _load_progress()
    if not progress:
        print("ERROR: rck-progress.json not found.", file=sys.stderr)
        sys.exit(1)

    progress["status"] = "completed"
    progress["committed"] = True
    _save_progress(progress)
    print("Pipeline marked as completed")


def cmd_progress_status(_args: argparse.Namespace) -> None:
    """Print current progress as JSON for resume detection."""
    progress = _load_progress()
    if not progress:
        print(json.dumps({"status": "not_found"}))
        sys.exit(0)

    # Enrich with summary
    summary = {
        "uid": progress.get("uid"),
        "status": progress.get("status"),
        "current_loop": progress.get("current_loop"),
        "current_iter": progress.get("current_iter"),
        "committed": progress.get("committed", False),
        "loops_summary": {},
    }
    for lp, data in progress.get("loops", {}).items():
        summary["loops_summary"][lp] = {
            "status": data.get("status"),
            "files_done": len(data.get("files_done", [])),
            "files_clean": len(data.get("files_clean", [])),
        }
        if "iter" in data:
            summary["loops_summary"][lp]["iter"] = data["iter"]

    print(json.dumps(summary, indent=2))


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Rechecker pipeline helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create index from files.txt")
    p.add_argument("--uid", required=True, help="6-char UUID for this run")

    p = sub.add_parser("groups", help="List groups as JSON")
    p.add_argument("--macro", help="Macro-group ID (default: first)")

    p = sub.add_parser("merge-iteration", help="Merge fix reports → iteration report")
    p.add_argument("--loop", required=True)
    p.add_argument("--iter", required=True)

    p = sub.add_parser("merge-loop", help="Merge iteration reports → loop report")
    p.add_argument("--loop", required=True)

    p = sub.add_parser("merge-final", help="Merge loop reports → final report")
    p.add_argument("--keep", action="store_true", help="Keep intermediate files")

    p = sub.add_parser("count-issues", help="Count issues in review reports")
    p.add_argument("--loop", required=True)
    p.add_argument("--iter", required=True)

    # Progress tracking subcommands
    p = sub.add_parser("progress-init", help="Initialize rck-progress.json")

    p = sub.add_parser("progress-update", help="Update pipeline progress")
    p.add_argument("--loop", required=True, help="Loop number (1-4)")
    p.add_argument("--action", required=True,
                   choices=["start-loop", "start-iter", "file-done", "file-clean", "end-loop"],
                   help="Action to record")
    p.add_argument("--iter", help="Iteration number (for start-iter)")
    p.add_argument("--fid", help="File ID (for file-done/file-clean)")

    p = sub.add_parser("progress-complete", help="Mark pipeline as completed")

    p = sub.add_parser("progress-status", help="Print current progress as JSON")

    args = parser.parse_args()
    {
        "init": cmd_init,
        "groups": cmd_groups,
        "merge-iteration": cmd_merge_iteration,
        "merge-loop": cmd_merge_loop,
        "merge-final": cmd_merge_final,
        "count-issues": cmd_count_issues,
        "progress-init": cmd_progress_init,
        "progress-update": cmd_progress_update,
        "progress-complete": cmd_progress_complete,
        "progress-status": cmd_progress_status,
    }[args.command](args)


if __name__ == "__main__":
    main()

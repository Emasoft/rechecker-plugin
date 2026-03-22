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
import re
import sys
from datetime import datetime
from pathlib import Path

RECHECKER_DIR = Path(".rechecker")
REPORTS_DIR = RECHECKER_DIR / "reports"
INDEX_FILE = RECHECKER_DIR / "index.json"

# File size thresholds for big vs small classification
BIG_FILE_LINES = 200
BIG_FILE_BYTES = 10_000

# Group size limits
MAX_BIG_PER_GROUP = 3
PREFER_BIG_PER_GROUP = 1
MAX_SMALL_PER_GROUP = 10
PREFER_SMALL_PER_GROUP = 7

# Agent limits
MAX_AGENTS = 20
MAX_FILES_PER_MACRO = MAX_AGENTS * MAX_SMALL_PER_GROUP  # 200


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


def _load_index() -> dict:
    if not INDEX_FILE.exists():
        print("ERROR: .rechecker/index.json not found. Run 'init' first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(INDEX_FILE.read_text())


def _save_index(index: dict) -> None:
    INDEX_FILE.write_text(json.dumps(index, indent=2) + "\n")


def _extract_fid(filename: str) -> str | None:
    """Extract FIDxxxxx from a filename like rck-...-[LP00001-IT00001-FID00003]-fix.md"""
    m = re.search(r"FID\d{5}", filename)
    return m.group(0) if m else None


def _find_reports(directory: Path, loop: str, iter_: str | None, fid: str | None, suffix: str) -> list[Path]:
    """Find report files by loop/iter/fid/suffix using regex (glob can't handle literal brackets)."""
    parts = [re.escape(f"LP{int(loop):05d}")]
    if iter_ is not None:
        parts.append(re.escape(f"IT{int(iter_):05d}"))
    if fid is not None:
        parts.append(re.escape(fid))
    # If fid is not specified, allow any trailing content before the closing bracket
    tag_pattern = r"\[" + "-".join(parts) + (r"[^\]]*\]" if fid is None else r"\]")
    full_pattern = re.compile(r".*" + tag_pattern + r"-" + re.escape(suffix))

    results = []
    if directory.is_dir():
        for f in directory.iterdir():
            if full_pattern.match(f.name):
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

    file_paths = [line.strip() for line in files_txt.read_text().splitlines() if line.strip()]
    if not file_paths:
        print("ERROR: No files in .rechecker/files.txt", file=sys.stderr)
        sys.exit(1)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Assign FIDs and measure sizes
    files: dict[str, dict] = {}
    for i, path_str in enumerate(file_paths, 1):
        fid = f"FID{i:05d}"
        p = Path(path_str)
        lines = _count_lines(p)
        size = _file_size(p)
        category = "big" if lines > BIG_FILE_LINES or size > BIG_FILE_BYTES else "small"
        files[fid] = {
            "path": path_str,
            "lines": lines,
            "bytes": size,
            "category": category,
        }

    # Split into macro-groups if >200 files
    fid_list = list(files.keys())
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
    }
    _save_index(index)

    # Print summary
    big_count = sum(1 for f in files.values() if f["category"] == "big")
    small_count = sum(1 for f in files.values() if f["category"] == "small")
    print(f"Initialized: {len(files)} files, {len(all_groups)} groups, {len(macro_groups)} macro-group(s)")
    print(f"  Big: {big_count}, Small: {small_count}")
    for mg_id, mg_gids in macro_groups.items():
        file_count = sum(len(all_groups[g]) for g in mg_gids)
        print(f"  {mg_id}: {len(mg_gids)} groups, {file_count} files")


def cmd_groups(args: argparse.Namespace) -> None:
    """List groups and their files as JSON."""
    index = _load_index()
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
        for pattern in ["*-review.json", "*-fix.md", "*-iteration.md", "*-loop.md"]:
            for f in REPORTS_DIR.glob(pattern):
                f.unlink()


def cmd_count_issues(args: argparse.Namespace) -> None:
    """Count total issues in review reports for a loop/iteration. Prints count. Exit 1 if >0."""
    review_reports = _find_reports(REPORTS_DIR, args.loop, args.iter, None, "review.json")

    total = 0
    for report in review_reports:
        try:
            data = json.loads(report.read_text())
            if isinstance(data, list):
                total += len(data)
        except (json.JSONDecodeError, OSError):
            pass

    print(str(total))
    if total > 0:
        sys.exit(1)


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

    args = parser.parse_args()
    {
        "init": cmd_init,
        "groups": cmd_groups,
        "merge-iteration": cmd_merge_iteration,
        "merge-loop": cmd_merge_loop,
        "merge-final": cmd_merge_final,
        "count-issues": cmd_count_issues,
    }[args.command](args)


if __name__ == "__main__":
    main()

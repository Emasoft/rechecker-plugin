#!/usr/bin/env python3
"""count-tokens.py — Count tokens via delta snapshots.

Takes two snapshots of cumulative session token usage (before and after),
then computes the difference to get exact consumption for a specific operation.

Parsing pipeline replicated from claude-devtools (matt1398/claude-devtools):
1. Parse all JSONL entries with type=assistant and message.usage (parseJsonlFile)
2. Deduplicate by requestId — keep only the LAST entry per requestId
   (Claude writes multiple streaming entries per API response with incrementally
   increasing output_tokens; only the last has the final correct counts)
3. Sum usage from deduplicated entries (calculateMetrics)

Uses mmap for zero-copy streaming — handles 2GB+ transcripts with screenshots.
Lines over 1MB are skipped (screenshot/base64 blobs never contain usage data).

Usage:
    python3 count-tokens.py --snapshot <output-file>   # save current totals
    python3 count-tokens.py --delta <before-file>      # print delta since snapshot

Output: JSON with per-model and total token counts.
"""

import json
import mmap as mmap_mod
import os
import sys
from pathlib import Path

MODEL_ALIASES = {
    "claude-opus-4-6[1m]": "claude-opus-4-6",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6",
}

TOKEN_KEYS = [
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "api_calls",
]

# Lines over 1MB are screenshot/base64 blobs — skip without reading
SKIP_THRESHOLD = 1_000_000


def find_current_transcripts() -> list[Path]:
    """Find JSONL transcripts for the current project only.

    Matches the exact encoded project path as the directory name prefix,
    NOT as a substring — avoids matching worktree project dirs that contain
    the same path segment (e.g. project--claude-worktrees-...).
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    project_dir_hint = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    # Claude encodes /Users/foo/bar as -Users-foo-bar
    encoded_exact = "-" + project_dir_hint.replace("/", "-").lstrip("-")
    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        # Exact match: dir name must BE the encoded path, not just contain it
        if project_dir.name != encoded_exact:
            continue
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)
    return results


def _parse_entries(path: Path) -> list[dict]:
    """Parse assistant entries with usage from a single JSONL file.

    Returns a list of dicts with: request_id, model, usage.
    Uses mmap + skip lines >1MB for memory safety.
    """
    entries: list[dict] = []

    try:
        f = open(path, "rb")  # noqa: SIM115
    except OSError:
        return entries

    try:
        size = f.seek(0, 2)
        if size == 0:
            f.close()
            return entries
        f.seek(0)
        mm = mmap_mod.mmap(f.fileno(), 0, access=mmap_mod.ACCESS_READ)
    except (OSError, ValueError):
        f.close()
        return entries

    try:
        pos = 0
        while pos < size:
            nl = mm.find(b"\n", pos)
            if nl == -1:
                nl = size
            line_start = pos
            line_len = nl - pos
            pos = nl + 1

            if line_len < 20 or line_len > SKIP_THRESHOLD:
                continue

            line_bytes = mm[line_start:line_start + line_len]
            try:
                entry = json.loads(line_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            if entry.get("type") != "assistant":
                continue

            msg = entry.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue

            entries.append({
                "request_id": entry.get("requestId"),
                "model": msg.get("model") or "unknown",
                "usage": usage,
            })
    finally:
        mm.close()
        f.close()

    return entries


def _deduplicate_by_request_id(entries: list[dict]) -> list[dict]:
    """Deduplicate streaming entries by requestId — keep only the last per ID.

    Exact replication of claude-devtools deduplicateByRequestId().
    Entries without requestId pass through unchanged.
    """
    last_index_by_rid: dict[str, int] = {}
    for i, e in enumerate(entries):
        rid = e["request_id"]
        if rid:
            last_index_by_rid[rid] = i

    if not last_index_by_rid:
        return entries

    return [
        e for i, e in enumerate(entries)
        if not e["request_id"] or last_index_by_rid.get(e["request_id"]) == i
    ]


def aggregate_all() -> dict[str, dict[str, int]]:
    """Parse all transcripts, deduplicate, and sum usage per model.

    Pipeline: parse entries → dedup by requestId → sum per model.
    Matches claude-devtools: parseJsonlFile → calculateMetrics.
    """
    # Collect all entries across all transcript files
    all_entries: list[dict] = []
    for t in find_current_transcripts():
        all_entries.extend(_parse_entries(t))

    # Deduplicate streaming entries (global across all files)
    deduped = _deduplicate_by_request_id(all_entries)

    # Sum per model
    counts: dict[str, dict[str, int]] = {}
    for e in deduped:
        model = MODEL_ALIASES.get(e["model"], e["model"])
        if model not in counts:
            counts[model] = {k: 0 for k in TOKEN_KEYS}
        c = counts[model]
        u = e["usage"]
        c["input_tokens"] += u.get("input_tokens", 0)
        c["output_tokens"] += u.get("output_tokens", 0)
        c["cache_read_input_tokens"] += u.get("cache_read_input_tokens", 0)
        c["cache_creation_input_tokens"] += u.get("cache_creation_input_tokens", 0)
        c["api_calls"] += 1

    return counts


def build_summary(all_counts: dict[str, dict[str, int]]) -> dict:
    """Build a flat summary dict from per-model counts."""
    summary: dict[str, int] = {k: 0 for k in TOKEN_KEYS}
    for mc in all_counts.values():
        for k in TOKEN_KEYS:
            summary[k] += mc[k]
    summary["total_tokens"] = (
        summary["input_tokens"]
        + summary["output_tokens"]
        + summary["cache_read_input_tokens"]
        + summary["cache_creation_input_tokens"]
    )
    return {
        "summary": summary,
        "by_model": {m: dict(c) for m, c in sorted(all_counts.items())},
    }


def compute_delta(
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Subtract before from after to get the delta per model."""
    delta: dict[str, dict[str, int]] = {}
    all_models = set(after.keys()) | set(before.keys())
    for model in all_models:
        a = after.get(model, {k: 0 for k in TOKEN_KEYS})
        b = before.get(model, {k: 0 for k in TOKEN_KEYS})
        d = {k: a.get(k, 0) - b.get(k, 0) for k in TOKEN_KEYS}
        if any(v > 0 for v in d.values()):
            delta[model] = d
    return delta


def main() -> None:
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print("Count tokens via delta snapshots.")
        print()
        print("Usage:")
        print("  python3 count-tokens.py --snapshot <output-file>")
        print("  python3 count-tokens.py --delta <before-file>")
        print()
        print("Parsing: requestId deduplication (per claude-devtools),")
        print("mmap streaming, lines >1MB skipped.")
        print()
        print("Workflow:")
        print("  1. Take a snapshot before the operation:")
        print("     python3 count-tokens.py --snapshot /tmp/before.json")
        print("  2. Run the operation (recheck, etc.)")
        print("  3. Compute the delta:")
        print("     python3 count-tokens.py --delta /tmp/before.json")
        print("     -> prints JSON with tokens consumed by the operation")
        sys.exit(0)

    if "--snapshot" in args:
        idx = args.index("--snapshot")
        if idx + 1 >= len(args):
            print("--snapshot requires an output file path", file=sys.stderr)
            sys.exit(1)
        out_path = args[idx + 1]
        all_counts = aggregate_all()
        with open(out_path, "w") as outf:
            json.dump({"by_model": {m: dict(c) for m, c in all_counts.items()}}, outf)
        print(json.dumps({"status": "snapshot saved", "file": out_path}))

    elif "--delta" in args:
        idx = args.index("--delta")
        if idx + 1 >= len(args):
            print("--delta requires the before-snapshot file path", file=sys.stderr)
            sys.exit(1)
        before_path = args[idx + 1]
        try:
            with open(before_path) as bf:
                before_data = json.load(bf)
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"error": f"Failed to read before-snapshot: {e}"}))
            sys.exit(1)

        before_counts = before_data.get("by_model", {})
        after_counts = aggregate_all()
        delta = compute_delta(before_counts, after_counts)
        result = build_summary(delta)
        result["scope"] = "delta"
        print(json.dumps(result, indent=2))

    else:
        print("Usage: python3 count-tokens.py --snapshot <file>", file=sys.stderr)
        print("       python3 count-tokens.py --delta <before-file>", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

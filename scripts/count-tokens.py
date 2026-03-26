#!/usr/bin/env python3
"""count-tokens.py — Count tokens via delta snapshots.

Takes two snapshots of cumulative session token usage (before and after),
then computes the difference to get exact consumption for a specific operation.

Parsing logic derived from claude-devtools (matt1398/claude-devtools):
- requestId deduplication: Claude writes multiple streaming entries per API call
  with the same requestId but incrementally increasing output_tokens.
  Only the last entry per requestId has the final, correct token counts.
- isSidechain filtering: skip internal tool routing messages
- <synthetic> model filtering: skip non-API synthetic entries

Uses mmap for zero-copy streaming — handles 2GB+ transcripts with screenshots.

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


def find_current_transcripts() -> list[Path]:
    """Find JSONL transcripts for the current project only."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    project_dir_hint = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    encoded_hint = project_dir_hint.replace("/", "-").lstrip("-")
    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if encoded_hint not in project_dir.name:
            continue
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)
    return results


def parse_transcript_cumulative(path: Path) -> dict[str, dict[str, int]]:
    """Sum ALL usage entries in a transcript with requestId deduplication.

    Claude Code writes multiple JSONL entries per API response during streaming,
    each with the same requestId but incrementally increasing output_tokens.
    Only the last entry per requestId has the final, correct counts.

    Also skips:
    - isSidechain entries (internal tool routing)
    - <synthetic> model entries (not real API calls)

    Uses mmap with 512-byte peek to skip multi-MB screenshot/base64 lines.
    """
    PEEK = 512

    # Two-pass approach (same as claude-devtools deduplicateByRequestId):
    # Pass 1: collect all entries with usage, keyed by requestId (last wins)
    # Pass 2: sum the deduplicated entries
    # For entries without requestId, they pass through directly.

    entries_by_request_id: dict[str, dict] = {}
    entries_no_request_id: list[dict] = []

    try:
        f = open(path, "rb")  # noqa: SIM115
    except OSError:
        return {}

    try:
        size = f.seek(0, 2)
        if size == 0:
            f.close()
            return {}
        f.seek(0)
        mm = mmap_mod.mmap(f.fileno(), 0, access=mmap_mod.ACCESS_READ)
    except (OSError, ValueError):
        f.close()
        return {}

    try:
        pos = 0
        while pos < size:
            nl = mm.find(b"\n", pos)
            if nl == -1:
                nl = size
            line_start = pos
            line_len = nl - pos
            pos = nl + 1

            if line_len < 20:
                continue

            # Peek at first 512 bytes for pre-filter
            peek_end = min(line_start + PEEK, line_start + line_len)
            peek = mm[line_start:peek_end]

            # Must contain "usage" to have token data
            if b'"usage"' not in peek:
                continue

            # Skip sidechain entries (internal tool routing)
            if b'"isSidechain":true' in peek or b'"isSidechain": true' in peek:
                continue

            # Parse the full line
            line_bytes = mm[line_start:line_start + line_len]
            try:
                entry = json.loads(line_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            if entry.get("type") != "assistant":
                continue

            # Skip sidechain (in case spacing didn't match peek)
            if entry.get("isSidechain"):
                continue

            msg = entry.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue

            # Skip synthetic entries (not real API calls)
            model: str = msg.get("model") or "unknown"
            if model == "<synthetic>":
                continue

            model = MODEL_ALIASES.get(model, model)

            # Build a compact record for dedup
            record = {
                "model": model,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            }

            # Dedup by requestId — last entry wins (streaming writes incremental updates)
            request_id = entry.get("requestId")
            if request_id:
                entries_by_request_id[request_id] = record
            else:
                entries_no_request_id.append(record)
    finally:
        mm.close()
        f.close()

    # Sum deduplicated entries
    counts: dict[str, dict[str, int]] = {}
    all_records = list(entries_by_request_id.values()) + entries_no_request_id

    for record in all_records:
        model = record["model"]
        if model not in counts:
            counts[model] = {k: 0 for k in TOKEN_KEYS}
        c = counts[model]
        c["input_tokens"] += record["input_tokens"]
        c["output_tokens"] += record["output_tokens"]
        c["cache_read_input_tokens"] += record["cache_read_input_tokens"]
        c["cache_creation_input_tokens"] += record["cache_creation_input_tokens"]
        c["api_calls"] += 1

    return counts


def aggregate_all() -> dict[str, dict[str, int]]:
    """Sum usage across all transcripts for the current project."""
    all_counts: dict[str, dict[str, int]] = {}
    for t in find_current_transcripts():
        counts = parse_transcript_cumulative(t)
        for model, mc in counts.items():
            if model not in all_counts:
                all_counts[model] = {k: 0 for k in TOKEN_KEYS}
            for k in TOKEN_KEYS:
                all_counts[model][k] += mc[k]
    return all_counts


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
        print("mmap streaming, sidechain/synthetic filtering.")
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

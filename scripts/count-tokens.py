#!/usr/bin/env python3
"""count-tokens.py — Count tokens via delta snapshots or isolated transcripts.

Three modes:
    --snapshot <file>              Save cumulative totals (project + worktrees)
    --delta <before-file>          Compute difference since a previous snapshot
    --transcripts <path> [...]     Sum tokens from specific .jsonl files or dirs

Streaming-only: reads only head (200B) + tail (1200B) of each JSONL line.
Never loads full lines into memory. Uses mmap for zero-copy access.
Handles 2GB+ transcripts with multi-MB screenshot lines.

Output: JSON with per-model and total token counts.
"""

import json
import mmap as mmap_mod
import os
import re
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

# Tail size: usage block is 604-850 bytes from end, requestId 363-558 from end,
# model ~812 from end when near usage. 1200 bytes covers all with margin.
TAIL_SIZE = 1200

# Lines over 2MB are screenshot/base64 blobs — skip entirely
SKIP_THRESHOLD = 2_000_000

# Head size: check for "assistant" type in first 200 bytes (covers most entries)
HEAD_SIZE = 200

# Regex patterns for extracting fields from the tail region
_RE_REQUEST_ID = re.compile(rb'"requestId"\s*:\s*"([^"]+)"')
_RE_MODEL = re.compile(rb'"model"\s*:\s*"([^"]+)"')
_RE_INPUT = re.compile(rb'"input_tokens"\s*:\s*(\d+)')
_RE_OUTPUT = re.compile(rb'"output_tokens"\s*:\s*(\d+)')
_RE_CACHE_READ = re.compile(rb'"cache_read_input_tokens"\s*:\s*(\d+)')
_RE_CACHE_CREATE = re.compile(rb'"cache_creation_input_tokens"\s*:\s*(\d+)')


def find_current_transcripts() -> list[Path]:
    """Find JSONL transcripts for the current project and its worktrees.

    Matches the exact encoded project directory plus any worktree
    directories (encoded as ``<project>--claude-worktrees-<id>``).
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    project_dir_hint = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    encoded_exact = "-" + project_dir_hint.replace("/", "-").lstrip("-")
    # Worktree dirs are encoded as the project path with the worktree
    # path appended, so they always start with the same prefix followed
    # by "--claude-worktrees-".
    worktree_prefix = encoded_exact + "--claude-worktrees-"
    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        name = project_dir.name
        if name != encoded_exact and not name.startswith(worktree_prefix):
            continue
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)
    return results


def _parse_entries(path: Path) -> list[dict]:
    """Extract assistant entries with usage from a single JSONL file.

    Reads only head (200 bytes) + tail (1200 bytes) of each line.
    Never loads full lines. Uses regex on raw bytes — no json.loads.
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

            if line_len < 100 or line_len > SKIP_THRESHOLD:
                continue

            # Read head to check for "assistant" type
            head_end = min(line_start + HEAD_SIZE, line_start + line_len)
            head = mm[line_start:head_end]

            # For lines where "type":"assistant" is past HEAD_SIZE (up to 72K),
            # we check the tail instead — the "stop_reason" field is only in
            # assistant entries and always appears near the usage block
            is_assistant = b'"assistant"' in head

            # Read the tail (last 1200 bytes) where usage data lives
            tail_start = max(line_start, line_start + line_len - TAIL_SIZE)
            tail = mm[tail_start:line_start + line_len]

            # If head didn't confirm assistant, check tail for stop_reason
            # (only assistant entries have stop_reason + usage)
            if not is_assistant:
                if b'"stop_reason"' not in tail:
                    continue

            # Must have input_tokens in the tail
            m_input = _RE_INPUT.search(tail)
            if not m_input:
                continue

            # Extract all token fields from tail
            input_tokens = int(m_input.group(1))
            m_output = _RE_OUTPUT.search(tail)
            output_tokens = int(m_output.group(1)) if m_output else 0
            m_cache_read = _RE_CACHE_READ.search(tail)
            cache_read = int(m_cache_read.group(1)) if m_cache_read else 0
            m_cache_create = _RE_CACHE_CREATE.search(tail)
            cache_create = int(m_cache_create.group(1)) if m_cache_create else 0

            # Extract requestId and model — metadata fields live in the
            # top-level JSON object (head region, ~byte 84-530) while token
            # counts are always in the tail usage block. Search head first
            # then tail to handle any line length without blind spots.
            m_rid = _RE_REQUEST_ID.search(head) or _RE_REQUEST_ID.search(tail)
            request_id = m_rid.group(1).decode("utf-8", errors="replace") if m_rid else None
            m_model = _RE_MODEL.search(head) or _RE_MODEL.search(tail)
            model = m_model.group(1).decode("utf-8", errors="replace") if m_model else "unknown"

            entries.append({
                "request_id": request_id,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            })
    finally:
        mm.close()
        f.close()

    return entries


def _deduplicate_by_request_id(entries: list[dict]) -> list[dict]:
    """Deduplicate streaming entries by requestId — keep only the last per ID."""
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


def _sum_entries(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Deduplicate and sum a list of parsed entries by model."""
    deduped = _deduplicate_by_request_id(entries)
    counts: dict[str, dict[str, int]] = {}
    for e in deduped:
        model = MODEL_ALIASES.get(e["model"], e["model"])
        if model not in counts:
            counts[model] = {k: 0 for k in TOKEN_KEYS}
        c = counts[model]
        c["input_tokens"] += e["input_tokens"]
        c["output_tokens"] += e["output_tokens"]
        c["cache_read_input_tokens"] += e["cache_read_input_tokens"]
        c["cache_creation_input_tokens"] += e["cache_creation_input_tokens"]
        c["api_calls"] += 1
    return counts


def aggregate_all() -> dict[str, dict[str, int]]:
    """Parse all transcripts, deduplicate, and sum usage per model."""
    all_entries: list[dict] = []
    for t in find_current_transcripts():
        all_entries.extend(_parse_entries(t))
    return _sum_entries(all_entries)


def aggregate_paths(paths: list[str]) -> dict[str, dict[str, int]]:
    """Parse specific JSONL files or directories and sum usage per model.

    Each path can be a .jsonl file or a directory (scanned recursively).
    Use this to get isolated token counts for a single worktree session
    without polluting results with unrelated concurrent work.
    """
    all_entries: list[dict] = []
    for p in paths:
        # Expand ~ — SubagentStop events may provide tilde paths
        target = Path(p).expanduser()
        if target.is_file() and target.suffix == ".jsonl":
            all_entries.extend(_parse_entries(target))
        elif target.is_dir():
            for jsonl in target.rglob("*.jsonl"):
                all_entries.extend(_parse_entries(jsonl))
    return _sum_entries(all_entries)


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
        print("Count tokens via delta snapshots or isolated transcript paths.")
        print()
        print("Usage:")
        print("  python3 count-tokens.py --snapshot <output-file>")
        print("  python3 count-tokens.py --delta <before-file>")
        print("  python3 count-tokens.py --transcripts <path> [<path>...]")
        print()
        print("Modes:")
        print("  --snapshot   Save cumulative totals (project + all worktrees)")
        print("  --delta      Compute difference since a previous snapshot")
        print("  --transcripts  Sum tokens from specific .jsonl files or dirs.")
        print("               Use to get isolated counts for a single worktree")
        print("               session without noise from concurrent work.")
        print()
        print("Streaming: reads only head (200B) + tail (1200B) of each line.")
        print("No json.loads. mmap zero-copy. Lines >2MB skipped.")
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

    elif "--transcripts" in args:
        idx = args.index("--transcripts")
        paths = args[idx + 1:]
        if not paths:
            print("--transcripts requires one or more .jsonl files or directories", file=sys.stderr)
            sys.exit(1)
        counts = aggregate_paths(paths)
        result = build_summary(counts)
        result["scope"] = "transcripts"
        result["paths"] = paths
        print(json.dumps(result, indent=2))

    else:
        print("Usage: python3 count-tokens.py --snapshot <file>", file=sys.stderr)
        print("       python3 count-tokens.py --delta <before-file>", file=sys.stderr)
        print("       python3 count-tokens.py --transcripts <path>...", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

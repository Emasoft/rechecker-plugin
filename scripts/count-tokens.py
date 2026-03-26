#!/usr/bin/env python3
"""count-tokens.py — Count tokens used in a time window or a worktree session.

Parses Claude Code JSONL transcript files to produce a token breakdown by model.

Usage:
    python3 count-tokens.py --since <ISO-timestamp>
    python3 count-tokens.py --since 2026-03-26T14:00:00
    python3 count-tokens.py --worktree <worktree-name>   (legacy mode)

Output: JSON with per-model and total token counts + estimated cost.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_create": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_create": 1.0},
}

MODEL_ALIASES = {
    "claude-opus-4-6[1m]": "claude-opus-4-6",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6",
}


def find_current_transcripts() -> list[Path]:
    """Find JSONL transcripts for the current project only.

    Uses CLAUDE_PROJECT_DIR env var to identify the project,
    then looks for the matching encoded project dir under ~/.claude/projects/.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    # Scope to the current project by matching the encoded CWD in the dir name
    project_dir_hint = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    # Claude encodes paths as -Users-foo-bar (dashes replace slashes)
    encoded_hint = project_dir_hint.replace("/", "-").lstrip("-")

    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        # Only match dirs that contain our project path encoding
        if encoded_hint not in project_dir.name:
            continue
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)
    return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)


def find_worktree_transcripts(worktree_name: str) -> list[Path]:
    """Find JSONL transcripts for a specific worktree."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if worktree_name not in project_dir.name:
            continue
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)
    return sorted(results)


def parse_transcript(
    path: Path,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Parse a JSONL transcript and return token counts by model.

    Uses mmap for zero-copy, OS-managed paging — same technique as the PSS
    Rust binary. Never loads the full file into memory. For each line:
    1. Peek at first 512 bytes for '"usage"' substring
    2. Skip multi-MB screenshot/base64 lines without reading them
    3. Only json.loads lines that pass the pre-filter
    """
    import mmap as mmap_mod

    counts: dict[str, dict[str, int]] = {}
    PEEK = 512

    try:
        f = open(path, "rb")  # noqa: SIM115 — kept open alongside mmap
    except OSError:
        return counts

    try:
        size = f.seek(0, 2)
        if size == 0:
            f.close()
            return counts
        f.seek(0)
        mm = mmap_mod.mmap(f.fileno(), 0, access=mmap_mod.ACCESS_READ)
    except (OSError, ValueError):
        f.close()
        return counts

    try:
        pos = 0
        while pos < size:
            # Find next newline
            nl = mm.find(b"\n", pos)
            if nl == -1:
                nl = size
            line_start = pos
            line_len = nl - pos
            pos = nl + 1

            if line_len < 20:
                continue

            # Peek at first PEEK bytes — "usage" appears early in assistant entries
            peek_end = min(line_start + PEEK, line_start + line_len)
            peek = mm[line_start:peek_end]

            if b'"usage"' not in peek:
                continue

            # Pre-filter passed — read the full line and parse
            line_bytes = mm[line_start:line_start + line_len]
            try:
                entry = json.loads(line_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            if entry.get("type") != "assistant":
                continue

            # Filter by timestamp window
            if since or until:
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if since and ts < since:
                            continue
                        if until and ts > until:
                            continue
                    except (ValueError, TypeError):
                        continue
                else:
                    continue

            msg = entry.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue

            model: str = msg.get("model") or "unknown"
            model = MODEL_ALIASES.get(model, model)

            if model not in counts:
                counts[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "api_calls": 0,
                }

            c = counts[model]
            c["input_tokens"] += usage.get("input_tokens", 0)
            c["output_tokens"] += usage.get("output_tokens", 0)
            c["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
            c["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
            c["api_calls"] += 1
    finally:
        mm.close()
        f.close()

    return counts


def estimate_cost(model: str, counts: dict[str, int]) -> float:
    """Estimate cost in USD for a model's token usage."""
    pricing = PRICING.get(model)
    if not pricing:
        for key, val in PRICING.items():
            if model.startswith(key.rsplit("-", 1)[0]):
                pricing = val
                break
    if not pricing:
        return 0.0

    cost = 0.0
    cost += counts["input_tokens"] * pricing["input"] / 1_000_000
    cost += counts["output_tokens"] * pricing["output"] / 1_000_000
    cost += counts["cache_read_input_tokens"] * pricing["cache_read"] / 1_000_000
    cost += counts["cache_creation_input_tokens"] * pricing["cache_create"] / 1_000_000
    return cost


def build_result(label: str, all_counts: dict[str, dict[str, int]]) -> dict:
    """Build the summary result dict."""
    total_input = sum(c["input_tokens"] for c in all_counts.values())
    total_output = sum(c["output_tokens"] for c in all_counts.values())
    total_cache_read = sum(c["cache_read_input_tokens"] for c in all_counts.values())
    total_cache_create = sum(c["cache_creation_input_tokens"] for c in all_counts.values())
    total_calls = sum(c["api_calls"] for c in all_counts.values())
    total_cost = sum(estimate_cost(m, c) for m, c in all_counts.items())

    return {
        "scope": label,
        "summary": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_create_tokens": total_cache_create,
            "total_tokens": total_input + total_output + total_cache_read + total_cache_create,
            "api_calls": total_calls,
            "estimated_cost_usd": round(total_cost, 4),
        },
        "by_model": {
            model: {**counts, "cost_usd": round(estimate_cost(model, counts), 4)}
            for model, counts in sorted(all_counts.items())
        },
    }


def main() -> None:
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print("Count tokens used in a time window or worktree session.")
        print()
        print("Usage:")
        print("  python3 count-tokens.py --since <ISO-timestamp> [--until <ISO-timestamp>]")
        print("  python3 count-tokens.py --worktree <name>")
        print()
        print("Options:")
        print("  --since <ts>     Count tokens from API calls after this timestamp")
        print("  --until <ts>     Count tokens from API calls before this timestamp")
        print("  --worktree <name>  Count tokens from a specific worktree session")
        print("  -h, --help       Show this help")
        sys.exit(0)

    if "--since" in args:
        idx = args.index("--since")
        if idx + 1 >= len(args):
            print("Usage: python3 count-tokens.py --since <ISO-timestamp>", file=sys.stderr)
            sys.exit(1)
        since_str = args[idx + 1]
        try:
            since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Invalid timestamp: {since_str}", file=sys.stderr)
            sys.exit(1)

        until: datetime | None = None
        if "--until" in args:
            u_idx = args.index("--until")
            if u_idx + 1 < len(args):
                try:
                    until = datetime.fromisoformat(args[u_idx + 1].replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                except ValueError:
                    print(f"Invalid --until timestamp: {args[u_idx + 1]}", file=sys.stderr)
                    sys.exit(1)

        transcripts = find_current_transcripts()
        if not transcripts:
            print(json.dumps({"error": "No transcripts found"}))
            sys.exit(1)

        all_counts: dict[str, dict[str, int]] = {}
        for t in transcripts:
            # Only check recent transcripts (modified after since)
            try:
                if datetime.fromtimestamp(t.stat().st_mtime, tz=timezone.utc) < since:
                    continue
            except OSError:
                continue

            counts = parse_transcript(t, since=since, until=until)
            for model, mc in counts.items():
                if model not in all_counts:
                    all_counts[model] = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "api_calls": 0,
                    }
                for key in all_counts[model]:
                    all_counts[model][key] += mc[key]

        label = f"since {since_str}"
        if until:
            label += f" until {args[args.index('--until') + 1]}"
        result = build_result(label, all_counts)
        print(json.dumps(result, indent=2))

    elif "--worktree" in args:
        idx = args.index("--worktree")
        if idx + 1 >= len(args):
            print("Usage: python3 count-tokens.py --worktree <name>", file=sys.stderr)
            sys.exit(1)
        wt_name = args[idx + 1]
        transcripts = find_worktree_transcripts(wt_name)
        if not transcripts:
            print(json.dumps({"error": f"No transcripts found for {wt_name}"}))
            sys.exit(1)

        all_counts = {}
        for t in transcripts:
            counts = parse_transcript(t)
            for model, mc in counts.items():
                if model not in all_counts:
                    all_counts[model] = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "api_calls": 0,
                    }
                for key in all_counts[model]:
                    all_counts[model][key] += mc[key]

        result = build_result(f"worktree:{wt_name}", all_counts)
        print(json.dumps(result, indent=2))

    else:
        print("Usage: python3 count-tokens.py --since <ISO-timestamp>", file=sys.stderr)
        print("       python3 count-tokens.py --worktree <name>", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

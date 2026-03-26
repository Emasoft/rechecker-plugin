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
) -> dict[str, dict[str, int]]:
    """Parse a JSONL transcript and return token counts by model.

    If `since` is set, only count entries with a timestamp >= since.
    """
    counts: dict[str, dict[str, int]] = {}

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue

                # Filter by timestamp if --since is used
                if since:
                    ts_str = entry.get("timestamp")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if ts < since:
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

    except OSError:
        pass

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
        print("  python3 count-tokens.py --since <ISO-timestamp>")
        print("  python3 count-tokens.py --worktree <name>")
        print()
        print("Options:")
        print("  --since <ts>     Count tokens from API calls after this timestamp")
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

            counts = parse_transcript(t, since=since)
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

        result = build_result(f"since {since_str}", all_counts)
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

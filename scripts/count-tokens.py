#!/usr/bin/env python3
"""count-tokens.py — Count tokens used by a rechecker worktree run.

Parses Claude Code JSONL transcript files (orchestrator + subagents)
to produce an accurate token breakdown by model.

Usage:
    python3 count-tokens.py <worktree-name>
    python3 count-tokens.py rck-020c93

Output: JSON with per-model and total token counts + estimated cost.
"""

import json
import sys
from pathlib import Path

# Pricing per million tokens (as of 2026-03)
PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_create": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create": 3.75},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_create": 1.0},
}

# Aliases
MODEL_ALIASES = {
    "claude-opus-4-6[1m]": "claude-opus-4-6",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6",
}


def find_transcripts(worktree_name: str) -> list[Path]:
    """Find all JSONL transcript files for a worktree session."""
    home = Path.home()
    # Claude Code stores projects under ~/.claude/projects/{encoded-path}/
    projects_dir = home / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    # Search for directories matching the worktree name
    results = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if worktree_name not in project_dir.name:
            continue
        # Find all JSONL files (orchestrator + subagents)
        for jsonl in project_dir.rglob("*.jsonl"):
            results.append(jsonl)

    return sorted(results)


def parse_transcript(path: Path) -> dict[str, dict[str, int]]:
    """Parse a JSONL transcript and return token counts by model."""
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

                msg = entry.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue

                model: str = msg.get("model") or "unknown"
                # Normalize model name
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
        # Try prefix match
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


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 count-tokens.py <worktree-name>", file=sys.stderr)
        sys.exit(1)

    worktree_name = sys.argv[1]
    transcripts = find_transcripts(worktree_name)

    if not transcripts:
        print(json.dumps({"error": f"No transcripts found for {worktree_name}"}))
        sys.exit(1)

    # Aggregate across all transcripts
    all_counts: dict[str, dict[str, int]] = {}
    transcript_details = []

    for t in transcripts:
        counts = parse_transcript(t)
        # Determine if this is the orchestrator or a subagent
        is_subagent = "subagents" in str(t)
        label = f"subagent:{t.stem}" if is_subagent else "orchestrator"

        detail_models: dict[str, dict] = {}  # type: ignore[type-arg]

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

            cost = estimate_cost(model, mc)
            detail_models[model] = {**mc, "cost_usd": round(cost, 4)}

        transcript_details.append({"file": str(t), "label": label, "models": detail_models})

    # Build summary
    total_input = sum(c["input_tokens"] for c in all_counts.values())
    total_output = sum(c["output_tokens"] for c in all_counts.values())
    total_cache_read = sum(c["cache_read_input_tokens"] for c in all_counts.values())
    total_cache_create = sum(c["cache_creation_input_tokens"] for c in all_counts.values())
    total_calls = sum(c["api_calls"] for c in all_counts.values())
    total_cost = sum(estimate_cost(m, c) for m, c in all_counts.items())

    result = {
        "worktree": worktree_name,
        "transcripts_found": len(transcripts),
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
        "by_transcript": transcript_details,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

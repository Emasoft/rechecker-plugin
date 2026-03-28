#!/usr/bin/env python3
"""log-subagent-tokens.py — SubagentStop hook that logs token usage.

Reads the SubagentStop event from stdin, extracts agent_transcript_path,
and runs count-tokens.py --transcripts on it to get isolated usage.
Appends a record to .rechecker/subagent-tokens.jsonl.

Designed to be wired as a SubagentStop hook so token usage is captured
automatically for every subagent/worktree, even long-running ones that
span compactions or sessions.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    # Read SubagentStop event from stdin
    raw = sys.stdin.read()
    if not raw.strip():
        return

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return

    transcript_path = event.get("agent_transcript_path", "")
    if not transcript_path:
        return

    agent_id = event.get("agent_id", "unknown")
    agent_type = event.get("agent_type", "unknown")

    # Run count-tokens.py --transcripts on the agent's transcript
    plugin_root = os.environ.get(
        "CLAUDE_PLUGIN_ROOT", str(Path(__file__).parent.parent)
    )
    count_script = Path(plugin_root) / "scripts" / "count-tokens.py"
    if not count_script.exists():
        return

    try:
        result = subprocess.run(
            [sys.executable, str(count_script), "--transcripts", transcript_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return

    if result.returncode != 0:
        return

    try:
        token_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return

    # Build log record
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "transcript_path": transcript_path,
        "tokens": token_data.get("summary", {}),
        "by_model": token_data.get("by_model", {}),
    }

    # Append to .rechecker/subagent-tokens.jsonl in the project dir
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    log_dir = Path(project_dir) / ".rechecker"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "subagent-tokens.jsonl"

    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    main()

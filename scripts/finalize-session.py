#!/usr/bin/env python3
"""finalize-session.py — Finalize a recheck session: count tokens, write history, move reports.

Usage:
    python3 finalize-session.py \
        --uuid <session-uuid> \
        --commit <commit-hash> \
        --start <ISO-timestamp> \
        --report-dir <temp-report-dir> \
        --files-reviewed <N> \
        --issues-found <N> \
        --issues-fixed <N> \
        --commit-made

Output: JSON summary to stdout (session record + token usage).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize a recheck session")
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp of session start")
    parser.add_argument("--report-dir", required=True, help="Temp report directory")
    parser.add_argument("--snapshot", required=True, help="Path to before-snapshot JSON from count-tokens.py")
    parser.add_argument("--files-reviewed", type=int, default=0)
    parser.add_argument("--issues-found", type=int, default=0)
    parser.add_argument("--issues-fixed", type=int, default=0)
    parser.add_argument("--commit-made", action="store_true")
    args = parser.parse_args()

    rechecker_dir = Path(".rechecker")
    rechecker_dir.mkdir(exist_ok=True)

    # 1. Count tokens via delta (snapshot before vs now)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", str(Path(__file__).parent.parent))
    count_script = Path(plugin_root) / "scripts" / "count-tokens.py"
    end_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    token_data = {}
    if count_script.exists() and Path(args.snapshot).exists():
        result = subprocess.run(
            [sys.executable, str(count_script), "--delta", args.snapshot],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            try:
                token_data = json.loads(result.stdout)
            except json.JSONDecodeError:
                token_data = {"error": "failed to parse token counter output"}

    # 2. Write token usage to report dir
    report_dir = Path(args.report_dir)
    if report_dir.is_dir():
        token_file = report_dir / "token-usage.json"
        with open(token_file, "w") as f:
            json.dump(token_data, f, indent=2)

    # 3. Build session record (end_ts already computed above for token counting)
    session = {
        "uuid": args.uuid,
        "commit": args.commit,
        "started": args.start,
        "finished": end_ts,
        "files_reviewed": args.files_reviewed,
        "issues_found": args.issues_found,
        "issues_fixed": args.issues_fixed,
        "commit_made": args.commit_made,
        "tokens": token_data.get("summary", {}),
    }

    # 4. Append to history.jsonl
    history_file = rechecker_dir / "history.jsonl"
    with open(history_file, "a") as f:
        f.write(json.dumps(session) + "\n")

    # 5. Move reports to permanent location
    permanent_dir = rechecker_dir / "reports" / args.uuid
    permanent_dir.mkdir(parents=True, exist_ok=True)

    if report_dir.is_dir():
        for item in report_dir.iterdir():
            dest = permanent_dir / item.name
            shutil.move(str(item), str(dest))
        # Remove empty temp dir
        try:
            report_dir.rmdir()
        except OSError:
            pass
        # Remove empty parent if it was reports_dev/
        try:
            report_dir.parent.rmdir()
        except OSError:
            pass

    # 6. Move LLM Externalizer outputs
    llm_output = Path("llm_externalizer_output")
    if llm_output.is_dir():
        for item in llm_output.iterdir():
            if "rck" in item.name or "review" in item.name:
                shutil.move(str(item), str(permanent_dir / item.name))

    # 7. Print summary
    print(json.dumps(session, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit in Bash commands, finds the git root where each
commit ran, launches claude --worktree --agent for each.
Runs async (hooks.json "async": true).

After all orchestrators finish, writes RECHECKER_MERGE_PENDING.md
for the main Claude and outputs a systemMessage for the user.

Naming convention for all files:
  rck-{YYYYMMDD_HHMMSS}_{UUID6}-{purpose}.{ext}
  e.g. rck-20260321_193000_a1b2c3-report.md
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_LINES: list[str] = []


def _log(msg: str) -> None:
    _LOG_LINES.append(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def _flush_log(project_dir: str) -> None:
    if not _LOG_LINES:
        return
    try:
        log_dir = Path(project_dir) / "reports_dev"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "rck-hook.log", "a") as f:
            f.write("\n".join(_LOG_LINES) + "\n")
    except OSError:
        pass


def parse_json_field(data: Any, field_path: str) -> str:
    val: Any = data
    for key in field_path.split("."):
        if isinstance(val, dict):
            val = val.get(key, "")
        else:
            return ""
    return str(val) if val else ""


def extract_git_commit_dirs(command: str, default_cwd: str) -> list[str]:
    """Extract all directories where git commit runs in a compound command."""
    parts = re.split(r"&&|;", command)
    current_cwd = default_cwd
    commit_dirs: list[str] = []
    for part in parts:
        part = part.strip()
        cd_match = re.match(r'cd\s+("([^"]+)"|\'([^\']+)\'|(\S+))', part)
        if cd_match:
            cd_path = cd_match.group(2) or cd_match.group(3) or cd_match.group(4)
            resolved = Path(cd_path).expanduser()
            if not resolved.is_absolute():
                resolved = Path(default_cwd) / resolved
            if resolved.is_dir():
                current_cwd = str(resolved)
                _log(f"  cd -> {current_cwd}")
        if re.search(r"\bgit\s+commit\b", part):
            commit_dirs.append(current_cwd)
            _log(f"  git commit in: {current_cwd}")
    return commit_dirs


def find_git_root(start: str) -> str | None:
    """Find the git root. Handles normal repos, submodules (.git file), nested subdirs."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    p = Path(start)
    for parent in [p, *p.parents]:
        git_path = parent / ".git"
        if git_path.is_dir() or git_path.is_file():
            return str(parent)
    return None


def _is_rechecker_worktree(cwd: str) -> bool:
    """Check if we're inside a rechecker worktree (prevents recursive triggering)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        branch = result.stdout.strip()
        return branch.startswith("worktree-rck-")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# Patterns that TLDR creates inside worktrees — must be gitignored to prevent
# them from being committed by the rechecker's final `git add -A && git commit`.
_TLDR_GITIGNORE_PATTERNS = [".tldr/", ".tldrignore", ".tldr_session_*"]


def _ensure_tldr_gitignored(git_root: str) -> None:
    """Append TLDR patterns to .gitignore if not already present."""
    gitignore = Path(git_root) / ".gitignore"
    try:
        existing = gitignore.read_text() if gitignore.exists() else ""
    except OSError:
        existing = ""
    existing_lines = set(existing.splitlines())
    missing = [p for p in _TLDR_GITIGNORE_PATTERNS if p not in existing_lines]
    if not missing:
        return
    with open(gitignore, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("# TLDR artifacts (added by rechecker)\n")
        for pattern in missing:
            f.write(pattern + "\n")


def main() -> None:
    raw = sys.stdin.read()
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    # Fast gates — no logging, no JSON parsing, exit immediately for non-commit calls
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = parse_json_field(hook_input, "tool_name")
    if tool_name != "Bash":
        sys.exit(0)

    command = parse_json_field(hook_input, "tool_input.command")
    if re.search(r"--amend", command):
        sys.exit(0)
    if not any(re.search(r"\bgit\s+commit\b", p) for p in re.split(r"&&|;|\|", command)):
        sys.exit(0)

    # Past the fast gates — this is a git commit. Start logging.
    cwd = (
        parse_json_field(hook_input, "tool_input.cwd")
        or parse_json_field(hook_input, "cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR", "")
        or os.getcwd()
    )
    _log("--- git commit detected ---")
    _log(f"cwd={cwd} command={command[:200]}")

    # CRITICAL: prevent recursive triggering — if we're inside a rechecker worktree, skip
    if _is_rechecker_worktree(cwd):
        _log("skip: inside rechecker worktree (preventing recursion)")
        _flush_log(project_dir)
        sys.exit(0)

    if not shutil.which("claude"):
        _log("skip: claude not on PATH")
        _flush_log(project_dir)
        sys.exit(0)

    _log("PASSED all gates")

    # Find all git roots where commits happened
    commit_dirs = extract_git_commit_dirs(command, cwd) or [cwd]
    _log(f"commit_dirs={commit_dirs}")

    git_roots: list[str] = []
    seen: set[str] = set()
    for d in commit_dirs:
        root = find_git_root(d)
        _log(f"  find_git_root({d}) -> {root}")
        if root and root not in seen:
            git_roots.append(root)
            seen.add(root)

    if not git_roots:
        _log("no git roots found")
        _flush_log(cwd)
        sys.exit(0)

    _log(f"launching orchestrator for {len(git_roots)} repo(s): {git_roots}")
    _flush_log(cwd)

    orchestrator = "rechecker-plugin:rechecker-orchestrator"

    # Track results for all repos to build the final message
    results: list[dict[str, str]] = []

    # Resolve plugin root once (for copying bundled scripts)
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", "")) or Path(__file__).resolve().parent.parent

    for root in git_roots:
        # Ensure TLDR artifacts won't pollute worktree commits
        _ensure_tldr_gitignored(root)

        # Copy merge script into .rechecker/ so Claude can find it without CLAUDE_PLUGIN_ROOT
        rechecker_dir = Path(root) / ".rechecker"
        rechecker_dir.mkdir(parents=True, exist_ok=True)
        merge_src = plugin_root / "scripts" / "merge-worktrees.sh"
        merge_dst = rechecker_dir / "merge-worktrees.sh"
        if merge_src.is_file():
            shutil.copy2(str(merge_src), str(merge_dst))
            merge_dst.chmod(0o755)

        # 6-char uuid ties all files from this run together
        uid = uuid.uuid4().hex[:6]
        # Worktree name uses uuid only (stays fixed for the run's lifetime)
        wt_name = f"rck-{uid}"
        branch_name = f"worktree-{wt_name}"

        def _rck_name(purpose: str, ext: str) -> str:
            """Generate rck-{now}_{uid}-{purpose}.{ext} with timestamp at write time."""
            now = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"rck-{now}_{uid}-{purpose}.{ext}"

        cmd = [
            "claude", "--worktree", wt_name,
            "--agent", orchestrator,
            "--dangerously-skip-permissions",
            "-p", "Run the full recheck pipeline on the latest commit.",
        ]
        _log(f"  worktree: {wt_name} (uid={uid})")
        _log(f"  cmd: {' '.join(cmd)}")
        _log(f"  cwd: {root}")
        _flush_log(cwd)

        reports_dev = Path(root) / "reports_dev"
        reports_dev.mkdir(parents=True, exist_ok=True)
        stderr_log = reports_dev / _rck_name("stderr", "log")
        with open(stderr_log, "a") as stderr_f:
            result = subprocess.run(cmd, cwd=root, stdout=subprocess.DEVNULL, stderr=stderr_f)
        _log(f"  claude exit code: {result.returncode}")

        # Copy report from worktree to reports_dev/
        # The final report can be in multiple locations depending on where
        # the orchestrator wrote it or if it was interrupted:
        #   1. worktree root: rck-*-report.md (normal completion)
        #   2. worktree/reports_dev/: rck-*-report.md (alternative)
        #   3. worktree/.rechecker/reports/: *-loop.md (partial — loops completed but not merged)
        wt_dir = Path(root) / ".claude" / "worktrees" / wt_name
        report_path = ""
        if wt_dir.is_dir():
            # Search locations in priority order
            search_dirs = [
                wt_dir,                         # worktree root
                wt_dir / "reports_dev",         # reports_dev inside worktree
                wt_dir / ".rechecker" / "reports",  # intermediate reports
            ]
            for search_dir in search_dirs:
                if report_path:
                    break
                if not search_dir.is_dir():
                    continue
                for report in sorted(search_dir.glob("rck-*-report.md")):
                    dest = reports_dev / _rck_name("report", "md")
                    shutil.copy2(str(report), str(dest))
                    report_path = str(dest)
                    _log(f"  copied report -> {dest.name}")
                    break  # take only the first (most recent) report

        results.append({
            "root": root,
            "branch": branch_name,
            "wt_name": wt_name,
            "uid": uid,
            "report": report_path,
            "exit_code": str(result.returncode),
        })

    # Write rck-*-merge-pending.md in each repo root for Claude to see
    for r in results:
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        merge_file = f"rck-{now}_{r['uid']}-merge-pending.md"
        merge_lines = [
            "# Rechecker: Merge Pending",
            "",
            "The rechecker plugin reviewed your latest commit and fixed bugs.",
            "",
            f"- **Worktree**: `{r['wt_name']}`",
            f"- **Branch with fixes**: `{r['branch']}`",
        ]
        if r["report"]:
            merge_lines.append(f"- **Report**: `{r['report']}`")
            merge_lines.append("")
            merge_lines.append("### Report Summary")
            merge_lines.append("")
            # Inline a brief excerpt from the report so Claude sees it immediately
            try:
                report_text = Path(r["report"]).read_text()
                # Extract first 30 lines or up to 2000 chars as summary
                summary = "\n".join(report_text.splitlines()[:30])
                if len(summary) > 2000:
                    summary = summary[:2000] + "\n... (truncated, read full report)"
                merge_lines.append(summary)
            except OSError:
                merge_lines.append("(report file not readable)")
        merge_lines.extend([
            "",
            "## What you must do",
            "",
            "**Read the full report** at the path above, then merge all rechecker fixes at once:",
            "",
            "```bash",
            f'cd "{r["root"]}" && bash .rechecker/merge-worktrees.sh',
            "```",
            "",
            "Or merge this branch individually:",
            "",
            "```bash",
            f'cd "{r["root"]}" && git merge {r["branch"]} --no-edit',
            "```",
            "",
            "If there are merge conflicts, resolve them yourself and commit.",
            "After merging, delete this file and the worktree branch:",
            "",
            "```bash",
            f"rm {merge_file} && git branch -d {r['branch']}",
            "```",
        ])
        notice_path = Path(r["root"]) / merge_file
        notice_path.write_text("\n".join(merge_lines) + "\n")
        _log(f"  wrote {notice_path}")

    # Build context for Claude (additionalContext) AND user (systemMessage)
    has_fixes = any(r["report"] for r in results)
    git_root = results[0]["root"]

    context_lines = [
        "",
        "=======================================================================",
        "  RECHECKER PLUGIN: CODE REVIEW COMPLETE — YOU MUST MERGE THE FIXES",
        "=======================================================================",
        "",
        "The rechecker plugin finished reviewing your recent commit(s).",
        "Bug fixes have been committed to worktree branches and are ready to merge.",
        "",
    ]

    for r in results:
        if r["report"]:
            context_lines.append(f"  - Branch `{r['branch']}` has fixes. Report: `{r['report']}`")
        else:
            context_lines.append(f"  - Branch `{r['branch']}`: code was clean, no fixes needed.")
    context_lines.append("")

    if has_fixes:
        context_lines.extend([
            ">>> REQUIRED ACTION: Run this command NOW to merge all fixes:",
            "",
            "```bash",
            f'cd "{git_root}" && bash .rechecker/merge-worktrees.sh',
            "```",
            "",
            "This merges all fix branches, cleans up worktrees, and deletes",
            "temporary branches and files. It is fully automated and non-blocking.",
            "",
            ">>> DO NOT SKIP THIS STEP. Unmerged rechecker branches accumulate",
            "    and waste disk space. The fixes will NOT appear in your code",
            "    until you merge them.",
            "",
            "Read the reports in docs_dev/ after merging to see what was fixed.",
        ])
    else:
        context_lines.append("No fixes needed — all reviewed code was clean.")

    context_lines.extend([
        "",
        "=======================================================================",
        "",
    ])

    context_msg = "\n".join(context_lines)

    # Output both additionalContext (for Claude) and systemMessage (for user terminal)
    output = {
        "hookSpecificOutput": {
            "additionalContext": context_msg,
        },
        "systemMessage": f"RECHECKER: Review done. Run: bash .rechecker/merge-worktrees.sh",
    }
    print(json.dumps(output))

    _flush_log(cwd)


if __name__ == "__main__":
    main()

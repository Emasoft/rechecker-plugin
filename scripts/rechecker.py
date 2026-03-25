#!/usr/bin/env python3
"""rechecker.py - PostToolUse hook entry point.

Detects git commit in Bash commands, accumulates changed files across
commits, and launches a recheck worktree when the batch threshold is met.
Runs async (hooks.json "async": true).

Batch threshold: >5 files OR >50KB total across accumulated commits.
Below threshold, files are saved to pending-files.json for next commit.

After the orchestrator finishes, writes merge-pending.md for the main
Claude and outputs additionalContext for the next conversation turn.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_LINES: list[str] = []

# Batch thresholds — only trigger recheck when enough files accumulate
BATCH_MIN_FILES = 5
BATCH_MIN_BYTES = 50_000  # 50KB


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
        # Check both current (rck-) and legacy (rechecker-) naming conventions
        return branch.startswith("worktree-rck-") or branch.startswith("worktree-rechecker-")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# Patterns that TLDR creates inside worktrees — must be gitignored
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


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to temp file, then os.rename()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".rck-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_commit_files(git_root: str, sha: str) -> list[dict[str, Any]]:
    """Get changed files from a commit with their sizes."""
    try:
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", "--diff-filter=d", sha],
            cwd=git_root, capture_output=True, text=True, timeout=10,
        )
        files = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(git_root) / line
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            files.append({"path": line, "bytes": size, "commit": sha})
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _load_pending(git_root: str) -> dict:  # type: ignore[type-arg]
    """Load pending-files.json (accumulated files across commits)."""
    pending_file = Path(git_root) / ".rechecker" / "pending-files.json"
    if pending_file.exists():
        try:
            result: dict = json.loads(pending_file.read_text())  # type: ignore[type-arg]
            return result
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": [], "total_bytes": 0, "total_files": 0}


def _save_pending(git_root: str, pending: dict) -> None:
    """Save pending-files.json atomically."""
    pending_file = Path(git_root) / ".rechecker" / "pending-files.json"
    _atomic_write_json(pending_file, pending)


def _clear_pending(git_root: str) -> None:
    """Clear pending-files.json after successful recheck."""
    pending_file = Path(git_root) / ".rechecker" / "pending-files.json"
    if pending_file.exists():
        pending_file.unlink()


def _check_threshold(pending: dict) -> bool:  # type: ignore[type-arg]
    """Check if accumulated files meet the batch threshold."""
    total_files: int = pending["total_files"]
    total_bytes: int = pending["total_bytes"]
    return total_files >= BATCH_MIN_FILES or total_bytes >= BATCH_MIN_BYTES


def _launch_recheck(root: str, pending: dict, plugin_root: Path) -> dict[str, str]:
    """Launch a rechecker worktree for the accumulated batch."""
    # Ensure TLDR artifacts are gitignored
    _ensure_tldr_gitignored(root)

    # Copy merge script
    rechecker_dir = Path(root) / ".rechecker"
    rechecker_dir.mkdir(parents=True, exist_ok=True)
    merge_src = plugin_root / "scripts" / "merge-worktrees.sh"
    merge_dst = rechecker_dir / "merge-worktrees.sh"
    if merge_src.is_file():
        shutil.copy2(str(merge_src), str(merge_dst))
        merge_dst.chmod(0o755)

    # Copy TLDR index if available (avoids reindexing in worktree)
    tldr_src = Path(root) / ".tldr"
    if tldr_src.is_dir():
        _log("  .tldr/ index found — will be available in worktree via git")

    # Write the batch file list so the orchestrator knows which files to review
    files_list = sorted(set(f["path"] for f in pending["files"]))
    batch_file = rechecker_dir / "batch-files.txt"
    batch_file.write_text("\n".join(files_list) + "\n")
    _log(f"  batch: {len(files_list)} files, {pending['total_bytes']} bytes")

    # Get HEAD SHA
    try:
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        head_sha = sha_result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        head_sha = "HEAD"

    # Generate worktree name
    uid = uuid.uuid4().hex[:6]
    wt_name = f"rck-{uid}"
    branch_name = f"worktree-{wt_name}"

    def _rck_name(purpose: str, ext: str) -> str:
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"rck-{now}_{uid}-{purpose}.{ext}"

    # Plugin-scoped agent reference
    orchestrator = "rechecker-plugin:rechecker-orchestrator"

    cmd = [
        "claude", "--worktree", wt_name,
        "--agent", orchestrator,
        "--dangerously-skip-permissions",
        "-p", f"Run the full recheck pipeline on commit {head_sha}. "
              f"Use .rechecker/batch-files.txt instead of git show for the file list.",
    ]
    _log(f"  worktree: {wt_name} (uid={uid})")
    _log(f"  cmd: {' '.join(cmd)}")
    _log(f"  cwd: {root}")
    _flush_log(root)

    reports_dev = Path(root) / "reports_dev"
    reports_dev.mkdir(parents=True, exist_ok=True)
    stderr_log = reports_dev / _rck_name("stderr", "log")
    with open(stderr_log, "a") as stderr_f:
        result = subprocess.run(cmd, cwd=root, stdout=subprocess.DEVNULL, stderr=stderr_f)
    _log(f"  claude exit code: {result.returncode}")

    # Count tokens from JSONL transcripts
    token_summary = ""
    try:
        count_script = plugin_root / "scripts" / "count-tokens.py"
        if count_script.is_file():
            count_result = subprocess.run(
                ["python3", str(count_script), wt_name],
                capture_output=True, text=True, timeout=30,
            )
            if count_result.returncode == 0:
                token_data = json.loads(count_result.stdout)
                s = token_data.get("summary", {})
                total_in = s.get("input_tokens", 0) + s.get("cache_read_tokens", 0) + s.get("cache_create_tokens", 0)
                total_out = s.get("output_tokens", 0)
                cost = s.get("estimated_cost_usd", 0)
                token_summary = f"{total_in:,} in / {total_out:,} out (${cost:.2f})"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    if token_summary:
        _log(f"  tokens: {token_summary}")

    # Copy report from worktree
    wt_dir = Path(root) / ".claude" / "worktrees" / wt_name
    report_path = ""
    if wt_dir.is_dir():
        search_dirs = [wt_dir, wt_dir / "reports_dev", wt_dir / ".rechecker" / "reports"]
        for search_dir in search_dirs:
            if report_path:
                break
            if not search_dir.is_dir():
                continue
            for report in sorted(search_dir.glob("rck-*-report.md"), reverse=True):
                dest = reports_dev / _rck_name("report", "md")
                shutil.copy2(str(report), str(dest))
                report_path = str(dest)
                _log(f"  copied report -> {dest.name}")
                break

    return {
        "root": root,
        "branch": branch_name,
        "wt_name": wt_name,
        "uid": uid,
        "report": report_path,
        "exit_code": str(result.returncode),
        "tokens": token_summary,
        "stderr_log": str(stderr_log),
        "files_count": str(len(files_list)),
    }


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

    # CRITICAL: prevent recursive triggering
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

    # Resolve plugin root once
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", "")) or Path(__file__).resolve().parent.parent

    # For each git root: accumulate files, check threshold, launch if ready
    results: list[dict[str, str]] = []

    for root in git_roots:
        # Get HEAD SHA
        try:
            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root,
                capture_output=True, text=True, timeout=5,
            )
            head_sha = sha_result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            head_sha = "HEAD"

        # Get changed files from this commit
        commit_files = _get_commit_files(root, head_sha)
        if not commit_files:
            _log(f"  no files in commit for {root}")
            continue

        # Load pending batch and add new files
        rechecker_dir = Path(root) / ".rechecker"
        rechecker_dir.mkdir(parents=True, exist_ok=True)
        pending = _load_pending(root)

        # Deduplicate: if same path already pending, update with latest commit
        existing_paths = {f["path"] for f in pending["files"]}
        for f in commit_files:
            if f["path"] not in existing_paths:
                pending["files"].append(f)
                existing_paths.add(f["path"])

        pending["total_files"] = len(pending["files"])
        pending["total_bytes"] = sum(f["bytes"] for f in pending["files"])

        _log(f"  pending: {pending['total_files']} files, {pending['total_bytes']} bytes")

        # Check threshold
        if not _check_threshold(pending):
            _log(f"  below threshold ({BATCH_MIN_FILES} files or {BATCH_MIN_BYTES} bytes) — accumulating")
            _save_pending(root, pending)
            _flush_log(root)
            continue

        # Threshold met — launch recheck
        _log("  THRESHOLD MET — launching recheck")
        r = _launch_recheck(root, pending, plugin_root)
        results.append(r)

        # Clear pending after launch
        _clear_pending(root)

    if not results:
        _log("no rechecks triggered (below threshold)")
        _flush_log(cwd)
        # No output — nothing for Claude to see
        sys.exit(0)

    # Write merge-pending files and build notification
    for r in results:
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        merge_file = f"rck-{now}_{r['uid']}-merge-pending.md"
        merge_lines = [
            "# Rechecker: Merge Pending",
            "",
            f"Reviewed {r['files_count']} file(s) from accumulated commits.",
            "",
            f"- **Worktree**: `{r['wt_name']}`",
            f"- **Branch with fixes**: `{r['branch']}`",
        ]
        if r.get("tokens"):
            merge_lines.append(f"- **Tokens**: {r['tokens']}")
        if r["report"]:
            merge_lines.append(f"- **Report**: `{r['report']}`")
            merge_lines.append("")
            merge_lines.append("### Report Summary")
            merge_lines.append("")
            try:
                report_text = Path(r["report"]).read_text()
                summary = "\n".join(report_text.splitlines()[:30])
                if len(summary) > 2000:
                    summary = summary[:2000] + "\n... (truncated)"
                merge_lines.append(summary)
            except OSError:
                merge_lines.append("(report file not readable)")
        merge_lines.extend([
            "",
            "## Merge the fixes",
            "",
            "```bash",
            f'cd "{r["root"]}" && bash .rechecker/merge-worktrees.sh',
            "```",
        ])
        notice_path = Path(r["root"]) / merge_file
        notice_path.write_text("\n".join(merge_lines) + "\n")
        _log(f"  wrote {notice_path}")

    # Build notification for Claude
    has_fixes = any(r["report"] for r in results)
    git_root = results[0]["root"]

    context_lines = [
        "",
        "=======================================================================",
        "  RECHECKER PLUGIN: CODE REVIEW COMPLETE — YOU MUST MERGE THE FIXES",
        "=======================================================================",
        "",
    ]

    for r in results:
        if r["exit_code"] != "0" and not r["report"]:
            context_lines.append(f"  - {r['wt_name']}: FAILED (exit {r['exit_code']})")
        elif r["report"]:
            context_lines.append(f"  - {r['wt_name']}: {r['files_count']} files reviewed, fixes ready")
        else:
            context_lines.append(f"  - {r['wt_name']}: code was clean")
        if r.get("tokens"):
            context_lines.append(f"    Tokens: {r['tokens']}")
    context_lines.append("")

    if has_fixes:
        context_lines.extend([
            ">>> Run this NOW:",
            "",
            "```bash",
            f'cd "{git_root}" && bash .rechecker/merge-worktrees.sh',
            "```",
        ])

    context_lines.extend(["", "=======================================================================", ""])

    output = {
        "additionalContext": "\n".join(context_lines),
        "systemMessage": "RECHECKER: Review done. Run: bash .rechecker/merge-worktrees.sh",
    }
    print(json.dumps(output))
    _flush_log(cwd)


if __name__ == "__main__":
    main()

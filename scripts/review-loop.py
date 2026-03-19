#!/usr/bin/env python3
"""review-loop.py - Core review loop.

Uses 'claude --worktree' for automatic worktree lifecycle management.
Claude Code creates worktrees at <project>/.claude/worktrees/<name>.
Auto-cleans if no changes, keeps if changes were committed.

KEY DESIGN: We do NOT pass a diff file to the worktree because untracked
files in the main working directory are NOT visible in worktrees. Instead,
the agent runs 'git diff' itself using the commit SHA (all git objects are
shared across worktrees). The agent also resets its worktree to match the
commit state with 'git reset --hard <SHA>' before reviewing.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def run_git(*args, cwd=None):
    """Run a git command, return (stdout, returncode)."""
    r = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, cwd=cwd
    )
    return r.stdout.strip(), r.returncode


def cleanup_worktree(wt_name, project_dir):
    """Clean up a Claude-managed worktree and its branch."""
    wt_path = Path(project_dir) / ".claude" / "worktrees" / wt_name
    branch_name = f"worktree-{wt_name}"

    # Prune stale worktree refs first
    run_git("worktree", "prune", cwd=project_dir)

    # Try proper removal first
    _, rc = run_git("worktree", "remove", "--force", str(wt_path), cwd=project_dir)
    if rc != 0:
        # Fallback: only remove if path is strictly under .claude/worktrees/
        expected_prefix = str(Path(project_dir) / ".claude" / "worktrees") + os.sep
        if wt_path.is_dir() and str(wt_path).startswith(expected_prefix):
            shutil.rmtree(str(wt_path), ignore_errors=True)

    run_git("branch", "-D", branch_name, cwd=project_dir)


def parse_issues_from_report(report_file):
    """Parse ISSUES_FOUND and ISSUES_FIXED from a report file.

    Returns (issues_found, issues_fixed, has_marker).
    If the marker line exists but has no numeric value, defaults to 1 (safer than 0).
    """
    issues_found = -1
    issues_fixed = 0
    has_marker = False

    if not Path(report_file).is_file():
        return issues_found, issues_fixed, has_marker

    try:
        content = Path(report_file).read_text()
    except OSError:
        return issues_found, issues_fixed, has_marker

    # Find the last ISSUES_FOUND: line
    for line in content.splitlines():
        if re.match(r"^ISSUES_FOUND:", line, re.IGNORECASE):
            has_marker = True
            nums = re.findall(r"\d+", line)
            if nums:
                issues_found = int(nums[0])
            else:
                # Marker exists but no numeric value - treat as 1 (safer than 0)
                issues_found = 1

    # Find the last ISSUES_FIXED: line
    for line in content.splitlines():
        if re.match(r"^ISSUES_FIXED:", line, re.IGNORECASE):
            nums = re.findall(r"\d+", line)
            if nums:
                issues_fixed = int(nums[0])

    # Belt-and-suspenders: ensure non-negative
    if issues_found < 0 and has_marker:
        issues_found = 1

    return issues_found, issues_fixed, has_marker


def main():
    # Parameters
    project_dir = sys.argv[1]
    commit_sha = sys.argv[2]
    current_branch = sys.argv[3]
    reports_dir = sys.argv[4]
    timestamp = sys.argv[5]
    plugin_root = sys.argv[6]

    max_passes = 30
    agent_file = str(Path(plugin_root) / "agents" / "code-reviewer.md")
    scan_script = str(Path(plugin_root) / "scripts" / "scan.sh")
    changed_files_script = str(Path(plugin_root) / "scripts" / "changed-files.py")

    # State tracking
    total_issues_found = 0
    total_issues_fixed = 0
    pass_summaries = []
    final_status = "unknown"
    consecutive_no_fix = 0
    max_consecutive_no_fix = 2
    pre_merge_sha = commit_sha
    review_target_sha = commit_sha

    for pass_num in range(1, max_passes + 1):
        wt_name = f"rechecker-{timestamp}-pass{pass_num}"
        worktree_path = Path(project_dir) / ".claude" / "worktrees" / wt_name
        wt_branch = f"worktree-{wt_name}"
        report_filename = f"rechecker_{timestamp}_pass{pass_num}.md"
        report_file = str(Path(reports_dir) / report_filename)

        # Clean up any leftover worktree from a previous failed run
        cleanup_worktree(wt_name, project_dir)

        # Check if there are changes to review
        os.chdir(project_dir)

        if pass_num == 1:
            # First pass: review the triggering commit
            out, rc = run_git("log", "-1", "--format=%s", commit_sha, cwd=project_dir)
            commit_msg = out if rc == 0 else "Unknown"
            diff_stat, rc = run_git("diff", "--stat", f"{commit_sha}~1..{commit_sha}", cwd=project_dir)
            if rc != 0 or not diff_stat:
                diff_stat, _ = run_git("show", "--stat", commit_sha, "--format=", cwd=project_dir)
            pass_target_sha = commit_sha
        else:
            commit_msg = f"Rechecker pass {pass_num - 1} fixes"
            diff_stat, _ = run_git("diff", "--stat", f"{pre_merge_sha}..{review_target_sha}", cwd=project_dir)
            pass_target_sha = review_target_sha

        if not diff_stat:
            final_status = "clean"
            pass_summaries.append(f"Pass {pass_num}: No changes to review")
            break

        # Build the prompt
        # Detect first commit (no parent) to avoid "bad revision SHA~1" error
        if pass_num == 1:
            _, rc = run_git("rev-parse", f"{commit_sha}~1", cwd=project_dir)
            if rc == 0:
                diff_command = f"git diff {commit_sha}~1..{commit_sha}"
            else:
                diff_command = f"git show --format='' {commit_sha}"
        else:
            diff_command = f"git diff {pre_merge_sha}..{pass_target_sha}"

        reset_command = f"git reset --hard {pass_target_sha}"
        changed_files_gen = f"python3 {changed_files_script} {pass_target_sha} .rechecker_changed_files.txt"

        review_prompt = f"""You are reviewing code in a git worktree. Follow these steps EXACTLY:

STEP 1: Run the automated linter and security scan with autofix.
  This is the FIRST thing you must do. It runs Super-Linter (40+ language linters),
  Semgrep (OWASP security rules with autofix), and TruffleHog (secret detection) via Docker.

  First, ensure the worktree has the right files checked out:
    {reset_command}

  Then generate the list of changed files and run the scan ONLY on those files.
  The scan report MUST go into a subdirectory (not the worktree root) to avoid
  polluting the worktree with untracked files that would be caught by git add -A.

    {changed_files_gen}
    mkdir -p .rechecker_scan_output
    bash {scan_script} --autofix --target-list .rechecker_changed_files.txt --scan-timeout 10800 --skip-pull -o .rechecker_scan_output .

  The changed-files.py helper generates a clean list (one path per line, excludes
  deleted files, handles first commits and merge commits). The --target-list flag
  tells scan.sh to scan only those files instead of the entire codebase.
  --scan-timeout 10800 allows up to 3 hours total for the scan.
  --skip-pull uses cached Docker images (avoids re-pulling on every pass).

  IMPORTANT: Do NOT run scan.sh without --target-list, as that would scan the
  entire codebase and autofix unrelated files.

  The script prints the scan report file path to stdout. Read that report file.
  After reading the report, clean up scan artifacts so they don't pollute the commit:
    rm -rf .rechecker_scan_output .rechecker_changed_files.txt

  If the scan fails (e.g. Docker not available, no changed files), just continue to STEP 2.
  The scan is a best-effort enhancement, not a hard requirement.
  If the scan auto-fixed files, note what was fixed. Those fixes are already applied in place.

STEP 2: View the code changes to review (the original commit diff):
  {diff_command}

STEP 3: Review every changed file thoroughly using the checklist in your agent instructions.
  Also review any remaining (unfixed) findings from the scan report in STEP 1.

STEP 4: Fix any issues you find by editing the source files.
  Do NOT re-fix things the scan already auto-fixed in STEP 1.

STEP 5: If you made fixes (in STEP 4) OR if the scan made fixes (in STEP 1), commit everything:
  git add -A && git commit -m 'rechecker: pass {pass_num} fixes'

STEP 6: Write your review report to: {report_filename}
  (Use the Write tool to save it in the current working directory.)
  Include a section for scan results (what the scan found, what it auto-fixed, what remains).

Context:
- Commit message: {commit_msg}
- Commit SHA: {commit_sha}
- Review pass: {pass_num} of {max_passes}

If you find NO issues AND the scan found NO issues, do NOT create a commit. Just write the report with ISSUES_FOUND: 0"""

        # Run headless Claude in a managed worktree
        # Retry logic for transient API errors (rate limits, server overload).
        os.chdir(project_dir)
        max_retries = 3
        retry_delay = 30
        claude_exit_code = 0

        claude_stderr_file = Path(project_dir) / f".rechecker_stderr_{timestamp}_pass{pass_num}.log"

        for retry in range(max_retries + 1):
            if retry > 0:
                wait_time = retry_delay * retry
                pass_summaries.append(f"Pass {pass_num}: API error, retry {retry}/{max_retries} after {wait_time}s")
                time.sleep(wait_time)
                cleanup_worktree(wt_name, project_dir)
                os.chdir(project_dir)

            claude_stderr_file = Path(project_dir) / f".rechecker_stderr_{timestamp}_pass{pass_num}.log"
            try:
                with open(claude_stderr_file, "w") as stderr_f:
                    r = subprocess.run(
                        ["claude", "--worktree", wt_name,
                         "--agent", agent_file,
                         "-p", review_prompt,
                         "--dangerously-skip-permissions"],
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_f,
                        cwd=project_dir
                    )
                claude_exit_code = r.returncode
            except FileNotFoundError:
                claude_exit_code = 127

            if claude_exit_code == 0:
                break

            # Check stderr for transient errors worth retrying
            stderr_content = ""
            try:
                stderr_content = claude_stderr_file.read_text()
            except OSError:
                pass

            is_transient = bool(re.search(
                r"rate.?limit|429|too many requests|overloaded|503|502|504|"
                r"server error|timeout|ECONNRESET|ETIMEDOUT|capacity",
                stderr_content, re.IGNORECASE
            ))

            if not is_transient:
                pass_summaries.append(f"Pass {pass_num}: API error (non-transient), skipping retries")
                break

            if retry == max_retries:
                pass_summaries.append(f"Pass {pass_num}: API error, max retries exhausted")

        # Clean up stderr log
        try:
            claude_stderr_file.unlink(missing_ok=True)
        except OSError:
            pass

        # Retrieve report from worktree
        wt_report_file = worktree_path / report_filename
        if wt_report_file.is_file():
            Path(reports_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(wt_report_file), report_file)

        # Also copy scan reports from the subdirectory if they exist
        wt_scan_dir = worktree_path / ".rechecker_scan_output"
        if wt_scan_dir.is_dir():
            for scan_file in list(wt_scan_dir.glob("*.json")) + list(wt_scan_dir.glob("*.log")):
                try:
                    shutil.copy2(str(scan_file), str(Path(reports_dir) / scan_file.name))
                except OSError:
                    pass

        # Parse issues from report
        issues_found, issues_fixed, report_has_marker = parse_issues_from_report(report_file)

        # If no valid report, treat as unknown issues (never assume clean)
        if not report_has_marker:
            if worktree_path.is_dir():
                wt_commits_out, rc = run_git(
                    "log", f"{current_branch}..{wt_branch}", "--oneline",
                    cwd=str(worktree_path)
                )
                wt_commits = len(wt_commits_out.splitlines()) if rc == 0 and wt_commits_out else 0
                if wt_commits > 0:
                    issues_found = 1
                    issues_fixed = 1
                else:
                    issues_found = 1
                    issues_fixed = 0
            else:
                issues_found = 1
                issues_fixed = 0

            Path(report_file).parent.mkdir(parents=True, exist_ok=True)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(report_file, "w") as f:
                f.write(f"# Rechecker Review Report - Pass {pass_num}\n\n")
                f.write(f"**Date**: {now_str}\n")
                f.write(f"**Commit**: {commit_sha[:8]}\n\n")
                f.write("## Summary\n")
                f.write("Review agent did not produce a valid report (missing ISSUES_FOUND marker).\n\n")
                f.write(f"ISSUES_FOUND: {issues_found}\n")
                f.write(f"ISSUES_FIXED: {issues_fixed}\n")

        total_issues_found += issues_found
        total_issues_fixed += issues_fixed
        pass_summaries.append(
            f"Pass {pass_num}: {issues_found} issues found, {issues_fixed} fixed. "
            f"Report: {Path(report_file).name}"
        )

        # If 0 issues found (from a valid report), we are done
        if issues_found == 0 and report_has_marker:
            final_status = "clean"
            cleanup_worktree(wt_name, project_dir)
            break

        # Check if worktree still exists (= reviewer made changes)
        if not worktree_path.is_dir():
            consecutive_no_fix += 1

            if consecutive_no_fix >= max_consecutive_no_fix:
                final_status = (
                    f"agent_bug: reviewer found issues but failed to commit "
                    f"fixes {consecutive_no_fix} times in a row"
                )
                pass_summaries.append(
                    f"Pass {pass_num}: No fixes committed - "
                    f"{consecutive_no_fix} consecutive failures, giving up"
                )
                break

            pass_summaries.append(f"Pass {pass_num}: No fixes committed by reviewer - retrying once more")
            if pass_num == max_passes:
                final_status = "max_passes_reached"
            continue

        # Worktree exists: check for actual commits
        wt_commits_out, rc = run_git(
            "log", f"{current_branch}..{wt_branch}", "--oneline",
            cwd=str(worktree_path)
        )
        worktree_commits = len(wt_commits_out.splitlines()) if rc == 0 and wt_commits_out else 0

        if worktree_commits == 0:
            consecutive_no_fix += 1
            cleanup_worktree(wt_name, project_dir)

            if consecutive_no_fix >= max_consecutive_no_fix:
                final_status = (
                    f"agent_bug: reviewer found issues but failed to commit "
                    f"fixes {consecutive_no_fix} times in a row"
                )
                pass_summaries.append(
                    f"Pass {pass_num}: No fixes committed - "
                    f"{consecutive_no_fix} consecutive failures, giving up"
                )
                break

            pass_summaries.append(f"Pass {pass_num}: No fixes committed by reviewer - retrying once more")
            if pass_num == max_passes:
                final_status = "max_passes_reached"
            continue

        # Reviewer committed fixes - reset the no-fix counter
        consecutive_no_fix = 0

        # Merge fixes from worktree branch back into main
        os.chdir(project_dir)

        porcelain_out, _ = run_git("status", "--porcelain", cwd=project_dir)
        if porcelain_out:
            final_status = f"error: working directory not clean before merge at pass {pass_num}"
            pass_summaries.append(f"Pass {pass_num}: Merge skipped - dirty working directory")
            cleanup_worktree(wt_name, project_dir)
            break

        # Record HEAD before merge (diff base for the next pass)
        pre_merge_sha, _ = run_git("rev-parse", "HEAD", cwd=project_dir)

        _, rc = run_git("merge", "--no-edit", wt_branch, cwd=project_dir)
        if rc == 0:
            review_target_sha, _ = run_git("rev-parse", "HEAD", cwd=project_dir)
            pass_summaries.append(f"Pass {pass_num}: Fixes merged successfully")
        else:
            run_git("merge", "--abort", cwd=project_dir)
            final_status = f"merge_conflict at pass {pass_num}"
            pass_summaries.append(f"Pass {pass_num}: MERGE CONFLICT - manual resolution needed")
            cleanup_worktree(wt_name, project_dir)
            break

        # Clean up this pass's worktree
        cleanup_worktree(wt_name, project_dir)

        if pass_num == max_passes:
            final_status = "max_passes_reached"

    # Write final summary report
    summary_file = str(Path(reports_dir) / f"rechecker_{timestamp}_summary.md")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(summary_file, "w") as f:
        f.write("# Rechecker Summary\n\n")
        f.write(f"**Date**: {now_str}\n")
        f.write(f"**Trigger commit**: {commit_sha[:8]}\n")
        f.write(f"**Branch**: {current_branch}\n")
        f.write(f"**Status**: {final_status}\n")
        f.write(f"**Total issues found**: {total_issues_found}\n")
        f.write(f"**Total issues fixed**: {total_issues_fixed}\n\n")
        f.write("## Pass Details\n")
        for summary in pass_summaries:
            f.write(f"- {summary}\n")
        f.write("\n## Report Files\n")
        reports_path = Path(reports_dir)
        for rf in sorted(reports_path.glob(f"rechecker_{timestamp}_pass*.md")):
            f.write(f"- {rf.name}\n")

    # Output summary to stdout (captured by rechecker.py)
    report_instruction = f"READ the summary report now: {summary_file}"

    if final_status == "clean":
        print(
            f"Review completed ({final_status}). {total_issues_found} total issues found "
            f"across all passes, {total_issues_fixed} fixed. All code changes verified clean. "
            f"{report_instruction}"
        )
    elif "agent_bug" in final_status:
        print(
            f"Review completed with AGENT BUG. {total_issues_found} issues found but reviewer "
            f"failed to commit fixes after retries. READ the per-pass reports and fix the issues "
            f"yourself: {reports_dir}/rechecker_{timestamp}_pass*.md -- then {report_instruction}"
        )
    elif "merge_conflict" in final_status:
        print(
            f"Review completed with MERGE CONFLICT. {total_issues_found} issues found, "
            f"{total_issues_fixed} fixed before conflict. Manual merge resolution needed. "
            f"{report_instruction}"
        )
    elif final_status == "max_passes_reached":
        print(
            f"Review completed (max {max_passes} passes reached). {total_issues_found} total "
            f"issues found, {total_issues_fixed} fixed. Some issues may remain. READ the per-pass "
            f"reports for remaining issues: {reports_dir}/rechecker_{timestamp}_pass*.md -- then "
            f"{report_instruction}"
        )
    else:
        print(
            f"Review completed ({final_status}). {total_issues_found} issues found, "
            f"{total_issues_fixed} fixed. {report_instruction}"
        )


if __name__ == "__main__":
    main()

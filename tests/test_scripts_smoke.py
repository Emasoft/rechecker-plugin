"""Smoke tests for every script in scripts/.

These are real tests (no mocks) that verify each script can be invoked
without crashing. The publish pipeline runs these before every push —
any failure blocks the release.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _run(args: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_triage_help() -> None:
    """triage.py --help prints the docstring and exits 0."""
    r = _run([str(SCRIPTS / "triage.py"), "--help"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "triage.py" in r.stdout
    assert "Recheck pipeline triage" in r.stdout


def test_triage_rejects_invalid_max_group_size() -> None:
    """triage.py rejects non-integer --max-group-size with exit 1."""
    r = _run([str(SCRIPTS / "triage.py"), "--max-group-size", "abc"])
    assert r.returncode == 1
    assert "must be an integer" in r.stderr


def test_triage_rejects_zero_max_group_size() -> None:
    """triage.py rejects --max-group-size 0 with exit 1."""
    r = _run([str(SCRIPTS / "triage.py"), "--max-group-size", "0"])
    assert r.returncode == 1
    assert "must be >= 1" in r.stderr


def test_count_tokens_help() -> None:
    """count-tokens.py --help prints usage and exits 0."""
    r = _run([str(SCRIPTS / "count-tokens.py"), "--help"])
    assert r.returncode == 0
    assert "--snapshot" in r.stdout
    assert "--delta" in r.stdout
    assert "--transcripts" in r.stdout


def test_count_tokens_no_args() -> None:
    """count-tokens.py with no args prints help (same as --help)."""
    r = _run([str(SCRIPTS / "count-tokens.py")])
    assert r.returncode == 0
    assert "--snapshot" in r.stdout


def test_count_tokens_snapshot_then_delta(tmp_path: Path) -> None:
    """count-tokens.py --snapshot then --delta round-trips correctly."""
    snap = tmp_path / "snap.json"
    r1 = _run([str(SCRIPTS / "count-tokens.py"), "--snapshot", str(snap)])
    assert r1.returncode == 0
    assert snap.is_file()
    data = json.loads(snap.read_text())
    assert "by_model" in data

    r2 = _run([str(SCRIPTS / "count-tokens.py"), "--delta", str(snap)])
    assert r2.returncode == 0
    delta = json.loads(r2.stdout)
    assert delta.get("scope") == "delta"
    assert "summary" in delta


def test_log_stop_failure_empty_stdin() -> None:
    """log-stop-failure.py with empty stdin exits 0 (no crash)."""
    r = _run([str(SCRIPTS / "log-stop-failure.py")], stdin="")
    assert r.returncode == 0


def test_log_stop_failure_malformed_json() -> None:
    """log-stop-failure.py with malformed JSON exits 0 (graceful)."""
    r = _run([str(SCRIPTS / "log-stop-failure.py")], stdin="not json{")
    assert r.returncode == 0


def test_log_subagent_tokens_empty_stdin() -> None:
    """log-subagent-tokens.py with empty stdin exits 0 (no crash)."""
    r = _run([str(SCRIPTS / "log-subagent-tokens.py")], stdin="")
    assert r.returncode == 0


def test_log_subagent_tokens_missing_transcript() -> None:
    """log-subagent-tokens.py with missing agent_transcript_path exits 0."""
    event = json.dumps({"hook_event_name": "SubagentStop", "agent_id": "x"})
    r = _run([str(SCRIPTS / "log-subagent-tokens.py")], stdin=event)
    assert r.returncode == 0


def test_finalize_session_help() -> None:
    """finalize-session.py --help exits 0 and prints usage."""
    r = _run([str(SCRIPTS / "finalize-session.py"), "--help"])
    assert r.returncode == 0
    assert "--uuid" in r.stdout
    assert "--commit" in r.stdout


def test_publish_help() -> None:
    """publish.py --help exits 0 and prints usage."""
    r = _run([str(SCRIPTS / "publish.py"), "--help"])
    assert r.returncode == 0
    assert "--patch" in r.stdout
    assert "--minor" in r.stdout
    assert "--major" in r.stdout


def test_publish_requires_bump_flag() -> None:
    """publish.py without a bump flag exits 2 (argparse error)."""
    r = _run([str(SCRIPTS / "publish.py")])
    assert r.returncode == 2
    assert "required" in r.stderr.lower() or "one of" in r.stderr.lower()


# --- Single-root reports path guard ----------------------------------------
# Every artifact this plugin writes must land under reports/rechecker/.
# These tests catch a regression where a rename or refactor splits output
# back into per-component top-level folders (reports/recheck/,
# reports/lint-filter/, reports/sonnet-code-fixer/, reports/stop-failure/).
# All sub-component subfolders are still allowed, but only as children
# of reports/rechecker/.

_PLUGIN_FILES_WITH_REPORT_PATHS = [
    SCRIPTS / "triage.py",
    SCRIPTS / "log-stop-failure.py",
    SCRIPTS / "finalize-session.py",
    REPO_ROOT / "agents" / "sonnet-code-fixer.md",
    REPO_ROOT / "agents" / "lint-filter.md",
    REPO_ROOT / "skills" / "recheck" / "SKILL.md",
]


def test_no_legacy_report_paths_in_plugin_files() -> None:
    """No plugin file may reference the pre-migration top-level report folders."""
    legacy = (
        "reports/recheck/",
        "reports/lint-filter",
        "reports/sonnet-code-fixer",
        "reports/stop-failure",
    )
    offenders: list[str] = []
    for path in _PLUGIN_FILES_WITH_REPORT_PATHS:
        text = path.read_text()
        for needle in legacy:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: contains '{needle}'")
    assert not offenders, (
        "All plugin output must live under reports/rechecker/. Offenders:\n"
        + "\n".join(offenders)
    )


def test_triage_writes_under_reports_rechecker() -> None:
    """triage.py source pins the per-session dir to reports/rechecker/."""
    text = (SCRIPTS / "triage.py").read_text()
    assert '"reports" / "rechecker"' in text, (
        'triage.py must build session dir from "reports" / "rechecker".'
    )


def test_log_stop_failure_writes_under_reports_rechecker() -> None:
    """log-stop-failure.py source pins the log dir to reports/rechecker/stop-failure."""
    text = (SCRIPTS / "log-stop-failure.py").read_text()
    assert '"reports" / "rechecker" / "stop-failure"' in text, (
        'log-stop-failure.py must build log dir from '
        '"reports" / "rechecker" / "stop-failure".'
    )


# --- publish.py uv.lock-staging guard --------------------------------------
# Regression guard for the v3.3.5 fix (commit 4d679eb): publish.py must
# stage "uv.lock" so the lockfile lands in the version-bump commit. Without
# this, every release ships with uv.lock one version behind pyproject.toml,
# the next `uv run` silently rewrites the lock, and the next publish run
# cuts an extra `chore: update uv.lock` housekeeping commit. The defensive
# comment in v3.3.6 (commit 6321a88) is the documentation; this test is
# the executable enforcement.


def test_publish_stages_uv_lock_in_version_files() -> None:
    """publish.py's stage_commit_and_push must include "uv.lock" in version_files."""
    text = (SCRIPTS / "publish.py").read_text()
    # Locate the version_files block and assert "uv.lock" is inside it.
    # The block is the only `version_files = [` literal in the file.
    marker = "version_files = ["
    start = text.find(marker)
    assert start != -1, "publish.py must declare a version_files list literal"
    end = text.find("]", start)
    assert end != -1, "version_files list literal must be closed"
    block = text[start : end + 1]
    assert '"uv.lock"' in block, (
        'publish.py version_files must include "uv.lock". Without this entry, '
        "every release leaves uv.lock one version behind pyproject.toml. "
        "See commit 4d679eb (v3.3.5) for the original fix and 6321a88 (v3.3.6) "
        "for the inline rationale comment that guards this entry."
    )

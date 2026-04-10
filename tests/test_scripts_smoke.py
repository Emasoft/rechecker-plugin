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

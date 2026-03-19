#!/usr/bin/env python3
"""Unified publish pipeline: test -> lint -> validate -> bump -> commit -> tag -> push.

Pipeline stages (all fail-fast — any failure aborts):
  1. Check working tree is clean
  2. Run tests (pytest)
  3. Lint files (ruff)
  4. Validate plugin (validate_plugin.py)
  5. Check version consistency across all sources
  6. Bump version in plugin.json, pyproject.toml, and __version__ vars
  7. Generate changelog (git-cliff)
  8. Commit, tag, push
  9. Create GitHub release (gh CLI)

Usage:
    uv run python scripts/publish.py --patch
    uv run python scripts/publish.py --minor
    uv run python scripts/publish.py --major
    uv run python scripts/publish.py --patch --dry-run
    uv run python scripts/publish.py --patch --skip-tests
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# -- ANSI colors ---------------------------------------------------------------

def _colors_ok() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_C = _colors_ok()
RED    = "\033[0;31m" if _C else ""
GREEN  = "\033[0;32m" if _C else ""
YELLOW = "\033[1;33m" if _C else ""
BLUE   = "\033[0;34m" if _C else ""
BOLD   = "\033[1m" if _C else ""
NC     = "\033[0m" if _C else ""


# -- Helpers -------------------------------------------------------------------

def cprint(msg: str) -> None:
    print(msg, flush=True)

def run(
    cmd: list[str], cwd: Path | None = None, *, check: bool = True, capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, stream output, fail-fast on error."""
    cprint(f"  {BLUE}$ {' '.join(cmd)}{NC}")
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                            capture_output=capture, timeout=300)
    if check and result.returncode != 0:
        cprint(f"  {RED}Command failed (exit {result.returncode}){NC}")
        sys.exit(result.returncode)
    return result

def get_repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, check=True)
    return Path(r.stdout.strip())


# -- Semver --------------------------------------------------------------------

def parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse 'X.Y.Z' into (major, minor, patch)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def bump_semver(current: str, bump_type: str) -> str | None:
    """Bump version by major/minor/patch. Returns new version string or None."""
    parsed = parse_semver(current)
    if not parsed:
        return None
    major, minor, patch = parsed
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return None


# -- Version readers/writers ---------------------------------------------------

def get_current_version(plugin_root: Path) -> str | None:
    """Read version from .claude-plugin/plugin.json."""
    pj = plugin_root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        return data.get("version")
    except (json.JSONDecodeError, OSError):
        return None

def update_plugin_json(root: Path, new_ver: str) -> tuple[bool, str]:
    """Write version to .claude-plugin/plugin.json."""
    pj = root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return False, "plugin.json not found"
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        data["version"] = new_ver
        pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True, f"plugin.json -> {new_ver}"
    except (json.JSONDecodeError, OSError) as e:
        return False, f"plugin.json update failed: {e}"

def update_pyproject_toml(root: Path, new_ver: str) -> tuple[bool, str]:
    """Write version to pyproject.toml."""
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return False, "pyproject.toml not found"
    try:
        content = pp.read_text(encoding="utf-8")
        updated = re.sub(
            r'^(version\s*=\s*")[^"]*(")',
            rf'\g<1>{new_ver}\2',
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if updated == content:
            return False, "pyproject.toml: version field not found"
        pp.write_text(updated, encoding="utf-8")
        return True, f"pyproject.toml -> {new_ver}"
    except OSError as e:
        return False, f"pyproject.toml update failed: {e}"

def update_python_versions(root: Path, new_ver: str) -> list[tuple[bool, str]]:
    """Update __version__ = '...' in all .py files under scripts/."""
    results: list[tuple[bool, str]] = []
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return results
    pattern = re.compile(r'^(__version__\s*=\s*["\'])([^"\']*)(["\']\s*)$', re.MULTILINE)
    for py_file in scripts_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if not pattern.search(content):
            continue
        updated = pattern.sub(rf"\g<1>{new_ver}\3", content)
        if updated != content:
            py_file.write_text(updated, encoding="utf-8")
            results.append((True, f"{py_file.relative_to(root)} -> {new_ver}"))
    return results

def check_version_consistency(root: Path) -> tuple[bool, str]:
    """Verify all version sources match."""
    versions: dict[str, str | None] = {}

    # plugin.json
    pj = root / ".claude-plugin" / "plugin.json"
    if pj.is_file():
        try:
            versions["plugin.json"] = json.loads(pj.read_text(encoding="utf-8")).get("version")
        except (json.JSONDecodeError, OSError):
            versions["plugin.json"] = None

    # pyproject.toml
    pp = root / "pyproject.toml"
    if pp.is_file():
        m = re.search(r'^version\s*=\s*"([^"]*)"', pp.read_text(encoding="utf-8"), re.MULTILINE)
        versions["pyproject.toml"] = m.group(1) if m else None

    found = {k: v for k, v in versions.items() if v is not None}
    if not found:
        return False, "No version sources found"
    unique = set(found.values())
    if len(unique) == 1:
        return True, f"All versions match: {unique.pop()}"
    details = ", ".join(f"{k}={v}" for k, v in found.items())
    return False, f"Version mismatch: {details}"

def do_bump(root: Path, new_ver: str, dry_run: bool = False) -> bool:
    """Orchestrate all version updates."""
    cprint(f"\n{BOLD}Bumping to {new_ver}{' (dry-run)' if dry_run else ''}{NC}")

    if dry_run:
        cprint(f"  Would update plugin.json -> {new_ver}")
        cprint(f"  Would update pyproject.toml -> {new_ver}")
        cprint(f"  Would update __version__ vars -> {new_ver}")
        return True

    ok1, msg1 = update_plugin_json(root, new_ver)
    cprint(f"  {'OK' if ok1 else 'FAIL'}: {msg1}")

    ok2, msg2 = update_pyproject_toml(root, new_ver)
    cprint(f"  {'OK' if ok2 else 'FAIL'}: {msg2}")

    py_results = update_python_versions(root, new_ver)
    for ok, msg in py_results:
        cprint(f"  {'OK' if ok else 'FAIL'}: {msg}")

    return ok1 and ok2


# -- Pipeline stages -----------------------------------------------------------

def stage_check_clean(root: Path) -> None:
    """Step 1: Working tree must be clean."""
    cprint(f"\n{BOLD}[1/9] Checking working tree...{NC}")
    r = run(["git", "status", "--porcelain"], cwd=root, capture=True)
    if r.stdout.strip():
        cprint(f"  {RED}Working tree is dirty. Commit or stash changes first.{NC}")
        cprint(r.stdout)
        sys.exit(1)
    cprint(f"  {GREEN}Clean.{NC}")

def stage_tests(root: Path) -> None:
    """Step 2: Run pytest."""
    cprint(f"\n{BOLD}[2/9] Running tests...{NC}")
    test_dir = root / "tests"
    if not test_dir.is_dir():
        cprint(f"  {YELLOW}No tests/ directory — skipping.{NC}")
        return
    run(["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=short"], cwd=root)
    cprint(f"  {GREEN}Tests passed.{NC}")

def stage_lint(root: Path) -> None:
    """Step 3: Lint with ruff."""
    cprint(f"\n{BOLD}[3/9] Linting...{NC}")
    run(["uv", "run", "ruff", "check", "scripts/"], cwd=root)
    cprint(f"  {GREEN}Lint passed.{NC}")

def stage_validate(root: Path) -> None:
    """Step 4: Validate plugin structure."""
    cprint(f"\n{BOLD}[4/9] Validating plugin...{NC}")
    validator = root / "scripts" / "validate_plugin.py"
    if not validator.is_file():
        cprint(f"  {YELLOW}No validate_plugin.py — skipping.{NC}")
        return
    run(["uv", "run", "python", str(validator), ".", "--strict"], cwd=root)
    cprint(f"  {GREEN}Validation passed.{NC}")

def stage_consistency(root: Path) -> None:
    """Step 5: Check version consistency."""
    cprint(f"\n{BOLD}[5/9] Checking version consistency...{NC}")
    ok, msg = check_version_consistency(root)
    cprint(f"  {msg}")
    if not ok:
        cprint(f"  {RED}Fix version mismatch before publishing.{NC}")
        sys.exit(1)
    cprint(f"  {GREEN}Consistent.{NC}")

def stage_bump(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 6: Bump version."""
    cprint(f"\n{BOLD}[6/9] Bumping version...{NC}")
    if not do_bump(root, new_ver, dry_run=dry_run):
        cprint(f"  {RED}Version bump failed.{NC}")
        sys.exit(1)
    cprint(f"  {GREEN}Version bumped to {new_ver}.{NC}")

def stage_changelog(root: Path, dry_run: bool) -> None:
    """Step 7: Generate changelog with git-cliff."""
    cprint(f"\n{BOLD}[7/9] Generating changelog...{NC}")
    if not shutil.which("git-cliff"):
        cprint(f"  {YELLOW}git-cliff not installed — skipping changelog.{NC}")
        return
    cliff_toml = root / "cliff.toml"
    if not cliff_toml.is_file():
        cprint(f"  {YELLOW}No cliff.toml — skipping changelog.{NC}")
        return
    if dry_run:
        cprint("  Would run: git-cliff -o CHANGELOG.md")
        return
    run(["git-cliff", "-o", "CHANGELOG.md"], cwd=root)
    cprint(f"  {GREEN}Changelog generated.{NC}")

def stage_commit_and_push(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 8: Commit, tag, push."""
    cprint(f"\n{BOLD}[8/9] Committing and pushing...{NC}")
    tag = f"v{new_ver}"
    if dry_run:
        cprint(f"  Would commit: chore: bump version to {new_ver}")
        cprint(f"  Would tag: {tag}")
        cprint("  Would push: origin HEAD --tags")
        return
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-m", f"chore: bump version to {new_ver}"], cwd=root)
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], cwd=root)
    run(["git", "push", "origin", "HEAD", "--tags"], cwd=root)
    cprint(f"  {GREEN}Pushed {tag}.{NC}")

def stage_gh_release(root: Path, new_ver: str, dry_run: bool) -> None:
    """Step 9: Create GitHub release via gh CLI."""
    cprint(f"\n{BOLD}[9/9] Creating GitHub release...{NC}")
    tag = f"v{new_ver}"
    if not shutil.which("gh"):
        cprint(f"  {YELLOW}gh CLI not installed — skipping release.{NC}")
        return
    if dry_run:
        cprint(f"  Would create release: {tag}")
        return
    changelog_file = root / "CHANGELOG.md"
    args = ["gh", "release", "create", tag, "--title", tag, "--generate-notes"]
    if changelog_file.is_file():
        args.extend(["--notes-file", str(changelog_file)])
    run(args, cwd=root, check=False)
    cprint(f"  {GREEN}Release created.{NC}")


# -- Main ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified publish pipeline for Claude Code plugins.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bump_group = parser.add_mutually_exclusive_group(required=True)
    bump_group.add_argument("--patch", action="store_const", dest="bump", const="patch")
    bump_group.add_argument("--minor", action="store_const", dest="bump", const="minor")
    bump_group.add_argument("--major", action="store_const", dest="bump", const="major")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest step")
    args = parser.parse_args()

    root = get_repo_root()
    current = get_current_version(root)
    if not current:
        cprint(f"{RED}Cannot read version from .claude-plugin/plugin.json{NC}")
        return 1

    new_ver = bump_semver(current, args.bump)
    if not new_ver:
        cprint(f"{RED}Cannot parse current version: {current}{NC}")
        return 1

    cprint(f"\n{BOLD}Publish pipeline: {current} -> {new_ver}{NC}")
    if args.dry_run:
        cprint(f"{YELLOW}(dry-run mode — no changes will be made){NC}")

    stage_check_clean(root)
    if not args.skip_tests:
        stage_tests(root)
    stage_lint(root)
    stage_validate(root)
    stage_consistency(root)
    stage_bump(root, new_ver, args.dry_run)
    stage_changelog(root, args.dry_run)
    stage_commit_and_push(root, new_ver, args.dry_run)
    stage_gh_release(root, new_ver, args.dry_run)

    cprint(f"\n{GREEN}{BOLD}Published {new_ver} successfully!{NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

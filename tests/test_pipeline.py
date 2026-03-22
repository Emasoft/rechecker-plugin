"""Tests for pipeline.py grouping, ID assignment, and report merging."""

import json
import sys
import tempfile
import os
from pathlib import Path

# Add scripts/ to path so we can import pipeline
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pipeline


def _create_project(tmp: Path, file_specs: list[tuple[str, int]]) -> Path:
    """Create a fake project with files. file_specs = [(path, line_count), ...]."""
    rechecker = tmp / ".rechecker"
    rechecker.mkdir(parents=True)
    (rechecker / "reports").mkdir()

    paths = []
    for rel_path, line_count in file_specs:
        p = tmp / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join([f"line {i}" for i in range(line_count)]) + "\n")
        paths.append(rel_path)

    (rechecker / "files.txt").write_text("\n".join(paths) + "\n")
    return tmp


def _run_init(tmp: Path, uid: str = "abc123") -> dict:
    """Run init in the given project dir and return the index."""
    orig = os.getcwd()
    try:
        os.chdir(tmp)
        pipeline.INDEX_FILE = Path(".rechecker/index.json")
        pipeline.RECHECKER_DIR = Path(".rechecker")
        pipeline.REPORTS_DIR = Path(".rechecker/reports")

        class Args:
            pass
        args = Args()
        args.uid = uid
        pipeline.cmd_init(args)
        return json.loads(Path(".rechecker/index.json").read_text())
    finally:
        os.chdir(orig)


def test_unique_fids_across_subfolders():
    """Each file in nested subfolders gets a unique FID."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [
            ("src/a/deep/file1.py", 10),
            ("src/a/deep/file2.py", 10),
            ("src/b/file3.py", 10),
            ("lib/utils/helper.py", 10),
            ("tests/test_x.py", 10),
        ]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        fids = list(index["files"].keys())
        assert len(fids) == 5, f"Expected 5 FIDs, got {len(fids)}"
        assert len(set(fids)) == 5, "FIDs are not unique"
        # Verify sequential numbering
        assert fids == ["FID00001", "FID00002", "FID00003", "FID00004", "FID00005"]


def test_small_files_grouped_together():
    """Small files (<=200 lines) are grouped 7 per group by default."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [(f"src/file{i}.py", 50) for i in range(21)]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        groups = index["groups"]
        # 21 small files / 7 preferred = 3 groups
        assert len(groups) == 3, f"Expected 3 groups, got {len(groups)}"
        assert len(groups["G001"]) == 7
        assert len(groups["G002"]) == 7
        assert len(groups["G003"]) == 7


def test_big_files_get_own_group():
    """Big files (>200 lines) get 1 per group by default."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [
            ("src/big1.py", 500),
            ("src/big2.py", 300),
            ("src/big3.py", 400),
        ]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        groups = index["groups"]
        assert len(groups) == 3, f"Expected 3 groups, got {len(groups)}"
        for gid, fids in groups.items():
            assert len(fids) == 1, f"Group {gid} should have 1 big file, got {len(fids)}"


def test_mixed_big_and_small():
    """Mix of big and small files creates appropriate groups."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [
            ("src/big1.py", 500),
            ("src/big2.py", 300),
        ] + [(f"src/small{i}.py", 50) for i in range(14)]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        groups = index["groups"]
        # 2 big files → 2 groups of 1
        # 14 small files → 2 groups of 7
        assert len(groups) == 4, f"Expected 4 groups, got {len(groups)}"


def test_max_20_groups():
    """Even with many big files, groups are capped at 20."""
    with tempfile.TemporaryDirectory() as tmp:
        # 30 big files — would be 30 groups at 1/group, must pack to ≤20
        specs = [(f"src/big{i}.py", 500) for i in range(30)]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        groups = index["groups"]
        assert len(groups) <= 20, f"Expected ≤20 groups, got {len(groups)}"
        # All 30 files should still be assigned
        total_files = sum(len(fids) for fids in groups.values())
        assert total_files == 30, f"Expected 30 files total, got {total_files}"


def test_macro_groups_over_200_files():
    """More than 200 files triggers macro-groups."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [(f"src/mod{i}/file{j}.py", 20) for i in range(25) for j in range(10)]
        # 250 small files
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        assert index["total_files"] == 250
        assert len(index["macro_groups"]) == 2, f"Expected 2 macro-groups, got {len(index['macro_groups'])}"

        # First macro-group should have ≤200 files
        mg1_files = sum(len(index["groups"][g]) for g in index["macro_groups"]["MG001"])
        mg2_files = sum(len(index["groups"][g]) for g in index["macro_groups"]["MG002"])
        assert mg1_files <= 200, f"MG001 has {mg1_files} files, expected ≤200"
        assert mg1_files + mg2_files == 250


def test_single_file():
    """A single file still works."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [("src/only.py", 10)]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        assert len(index["files"]) == 1
        assert len(index["groups"]) == 1
        assert index["groups"]["G001"] == ["FID00001"]


def test_file_categories():
    """Files are correctly classified as big or small."""
    with tempfile.TemporaryDirectory() as tmp:
        specs = [
            ("small.py", 50),      # small
            ("big_lines.py", 300), # big (lines)
        ]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        assert index["files"]["FID00001"]["category"] == "small"
        assert index["files"]["FID00002"]["category"] == "big"


def test_merge_iteration():
    """Fix reports are merged into an iteration report."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rechecker = tmp / ".rechecker"
        rechecker.mkdir()
        reports = rechecker / "reports"
        reports.mkdir()

        # Create index
        index = {
            "uid": "abc123",
            "files": {
                "FID00001": {"path": "src/a.py"},
                "FID00002": {"path": "src/b.py"},
            },
        }
        (rechecker / "index.json").write_text(json.dumps(index))

        # Create fix reports
        (reports / "rck-20260321_120000_abc123-[LP00001-IT00001-FID00001]-fix.md").write_text(
            "Fixed null check in line 42\n"
        )
        (reports / "rck-20260321_120001_abc123-[LP00001-IT00001-FID00002]-fix.md").write_text(
            "Fixed type error in line 15\n"
        )

        orig = os.getcwd()
        try:
            os.chdir(tmp)
            pipeline.INDEX_FILE = Path(".rechecker/index.json")
            pipeline.RECHECKER_DIR = Path(".rechecker")
            pipeline.REPORTS_DIR = Path(".rechecker/reports")

            class Args:
                loop = "1"
                iter = "1"
            pipeline.cmd_merge_iteration(Args())

            iter_reports = list(reports.glob("*-iteration.md"))
            assert len(iter_reports) == 1, f"Expected 1 iteration report, got {len(iter_reports)}"
            content = iter_reports[0].read_text()
            assert "src/a.py" in content
            assert "src/b.py" in content
            assert "null check" in content
            assert "type error" in content
        finally:
            os.chdir(orig)


def test_merge_loop():
    """Iteration reports are merged into a loop report."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rechecker = tmp / ".rechecker"
        rechecker.mkdir()
        reports = rechecker / "reports"
        reports.mkdir()

        index = {"uid": "abc123", "files": {}}
        (rechecker / "index.json").write_text(json.dumps(index))

        (reports / "rck-20260321_120000_abc123-[LP00002-IT00001]-iteration.md").write_text(
            "# Iteration 1\nFixed 3 issues\n"
        )
        (reports / "rck-20260321_120100_abc123-[LP00002-IT00002]-iteration.md").write_text(
            "# Iteration 2\nFixed 1 issue\n"
        )

        orig = os.getcwd()
        try:
            os.chdir(tmp)
            pipeline.INDEX_FILE = Path(".rechecker/index.json")
            pipeline.RECHECKER_DIR = Path(".rechecker")
            pipeline.REPORTS_DIR = Path(".rechecker/reports")

            class Args:
                loop = "2"
            pipeline.cmd_merge_loop(Args())

            loop_reports = list(reports.glob("*-loop.md"))
            assert len(loop_reports) == 1
            content = loop_reports[0].read_text()
            assert "Iteration 1" in content
            assert "Iteration 2" in content
            assert "[LP00002]" in loop_reports[0].name
        finally:
            os.chdir(orig)


def test_count_issues():
    """Count issues from review JSON files."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        reports = tmp / ".rechecker" / "reports"
        reports.mkdir(parents=True)

        # 3 issues in file 1, 0 in file 2
        (reports / "rck-20260321_120000_abc123-[LP00002-IT00001-FID00001]-review.json").write_text(
            json.dumps([
                {"file": "a.py", "line": 10, "severity": "high", "description": "bug1"},
                {"file": "a.py", "line": 20, "severity": "low", "description": "bug2"},
                {"file": "a.py", "line": 30, "severity": "medium", "description": "bug3"},
            ])
        )
        (reports / "rck-20260321_120000_abc123-[LP00002-IT00001-FID00002]-review.json").write_text("[]")

        orig = os.getcwd()
        try:
            os.chdir(tmp)
            pipeline.RECHECKER_DIR = Path(".rechecker")
            pipeline.REPORTS_DIR = Path(".rechecker/reports")
            pipeline.INDEX_FILE = Path(".rechecker/index.json")

            class Args:
                loop = "2"
                iter = "1"

            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            exit_code = 0
            with redirect_stdout(buf):
                try:
                    pipeline.cmd_count_issues(Args())
                except SystemExit as e:
                    exit_code = e.code
            assert exit_code == 1, f"Should exit 1 when issues found, got {exit_code}. stdout='{buf.getvalue()}'"
            assert buf.getvalue().strip() == "3", f"Expected '3', got '{buf.getvalue().strip()}'"
        finally:
            os.chdir(orig)


def test_extract_fid():
    """FID extraction from filenames works correctly."""
    assert pipeline._extract_fid("rck-20260321_120000_abc123-[LP00001-IT00001-FID00042]-fix.md") == "FID00042"
    assert pipeline._extract_fid("rck-20260321_120000_abc123-[LP00001-IT00001-FID00001]-review.json") == "FID00001"
    assert pipeline._extract_fid("no-fid-here.md") is None


def test_small_files_pack_to_10_when_needed():
    """When group count would exceed 20, small files pack up to 10."""
    with tempfile.TemporaryDirectory() as tmp:
        # 15 big files (15 groups) + 50 small files
        # 50/7 = 8 groups → total 23 > 20
        # Should pack small files to 10/group → 50/10 = 5 → total 20
        specs = [(f"big{i}.py", 500) for i in range(15)]
        specs += [(f"small{i}.py", 50) for i in range(50)]
        _create_project(Path(tmp), specs)
        index = _run_init(Path(tmp))

        groups = index["groups"]
        assert len(groups) <= 20, f"Expected ≤20 groups, got {len(groups)}"
        total = sum(len(fids) for fids in groups.values())
        assert total == 65


if __name__ == "__main__":
    # Simple test runner
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test in tests:
        name = test.__name__
        try:
            test()
            passed += 1
            print(f"  PASS  {name}: {test.__doc__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {test.__doc__}")
            traceback.print_exc()
            print()

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)

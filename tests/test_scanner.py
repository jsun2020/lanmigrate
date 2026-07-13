"""Scanner tests: context-aware exclusion positive/negative cases (PRD F3)."""
from pathlib import Path

import pytest

from lanmigrate import scanner


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Fixture tree:
    proj_node/         package.json + node_modules (EXCLUDE) + src
    proj_py/           pyproject.toml + .venv (EXCLUDE)
    plain/build/       no marker -> build must NOT be excluded
    proj_node/dist     marker present -> excluded
    """
    node = tmp_path / "proj_node"
    (node / "node_modules" / "lodash").mkdir(parents=True)
    (node / "node_modules" / "lodash" / "index.js").write_bytes(b"x" * 1000)
    (node / "dist").mkdir()
    (node / "dist" / "bundle.js").write_bytes(b"x" * 500)
    (node / "src").mkdir()
    (node / "src" / "app.js").write_bytes(b"x" * 100)
    (node / "package.json").write_text("{}")

    py = tmp_path / "proj_py"
    (py / ".venv" / "Lib").mkdir(parents=True)
    (py / ".venv" / "Lib" / "big.dll").write_bytes(b"x" * 2000)
    (py / "pyproject.toml").write_text("")
    (py / "main.py").write_text("print(1)")

    plain = tmp_path / "plain"
    (plain / "build").mkdir(parents=True)
    (plain / "build" / "keep_me.txt").write_bytes(b"x" * 300)

    return tmp_path


def test_node_modules_excluded_with_marker(tree: Path):
    report = scanner.scan(tree)
    rels = {e.rel for e in report.exclusions}
    assert "proj_node/node_modules" in rels
    assert "proj_node/dist" in rels
    assert "proj_py/.venv" in rels


def test_bare_build_dir_not_excluded(tree: Path):
    report = scanner.scan(tree)
    rels = {e.rel for e in report.exclusions}
    assert "plain/build" not in rels


def test_sizes_and_savings(tree: Path):
    report = scanner.scan(tree)
    by_rel = {e.rel: e for e in report.exclusions}
    assert by_rel["proj_node/node_modules"].size == 1000
    assert by_rel["proj_py/.venv"].size == 2000
    assert report.saved_bytes == 1000 + 500 + 2000
    # total includes excluded + kept files
    assert report.total_bytes >= report.saved_bytes + 300 + 100
    assert report.transfer_bytes == report.total_bytes - report.saved_bytes


def test_exclusion_records_rule_and_marker(tree: Path):
    report = scanner.scan(tree)
    node = next(e for e in report.exclusions if e.rel == "proj_node/node_modules")
    assert node.rule == "Node.js"
    assert node.marker == "package.json"


def test_filter_lines_rooted_and_global(tree: Path):
    report = scanner.scan(tree)
    lines = scanner.build_filter_lines(report)
    assert "- /proj_node/node_modules/**" in lines
    assert "- /plain/build/**" not in " ".join(lines)
    assert "- Thumbs.db" in lines
    assert "- __pycache__/**" in lines


def test_filter_lines_respects_user_selection(tree: Path):
    report = scanner.scan(tree)
    lines = scanner.build_filter_lines(report, enabled={"proj_py/.venv"})
    assert "- /proj_py/.venv/**" in lines
    assert "- /proj_node/node_modules/**" not in lines


def test_filter_escaping():
    assert scanner._escape_filter("a[1]/b*") == "a\\[1\\]/b\\*"

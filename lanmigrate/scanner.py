"""Directory scanner + context-aware exclusion rule engine (PRD F3, C.3-2).

A dependency directory is excluded only when one of its rule's marker files
exists in the SAME directory. All rules come from rules.toml; no directory
name is hard-coded here.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RULES_FILE = Path(__file__).parent / "rules.toml"

# Persisted scan results (PRD F18): re-sending an already-scanned folder can
# skip the (minutes-long on huge trees) re-walk. Only exclusion SUGGESTIONS
# come from the cache - the transfer itself always copies live contents.
SCANS_DIR = Path.home() / ".lanmigrate" / "scans"

# .git is never auto-excluded (history is valuable, PRD F3); we only report
# large ones so the user can decide.
GIT_REPORT_THRESHOLD = 1 << 30  # 1 GB


@dataclass
class Rule:
    name: str
    markers: list[str]
    exclude: list[str]


@dataclass
class Exclusion:
    path: Path  # absolute path of the excluded directory
    rel: str  # posix path relative to scan root
    rule: str  # rule name, e.g. "Node.js"
    marker: str  # the marker file that triggered it
    size: int  # bytes; -1 = unknown (fast scan skips sizing)


@dataclass
class ScanReport:
    root: Path
    exclusions: list[Exclusion] = field(default_factory=list)
    global_patterns: list[str] = field(default_factory=list)
    total_bytes: int = 0  # all files under root (excluded dirs included)
    file_count: int = 0
    large_git_dirs: list[tuple[str, int]] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return sum(e.size for e in self.exclusions if e.size > 0)

    @property
    def transfer_bytes(self) -> int:
        return max(0, self.total_bytes - self.saved_bytes)


def load_rules(path: Path = RULES_FILE) -> tuple[list[Rule], list[str]]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    rules = [Rule(r["name"], list(r["markers"]), list(r["exclude"]))
             for r in data.get("rules", [])]
    global_patterns = list(data.get("global", {}).get("exclude", []))
    return rules, global_patterns


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def scan(root: Path, rules_file: Path = RULES_FILE,
         on_progress=None, compute_sizes: bool = True) -> ScanReport:
    """Walk `root` applying exclusion rules. `on_progress(files, bytes, rel_dir)`
    is called once per directory so the CLI can show the scan is alive on
    large trees (a full stat pass over a big disk takes minutes).

    compute_sizes=False is the fast-start mode: only the directory structure
    is walked (no per-file stat, no sizing of excluded dirs), so the exclusion
    list is ready in seconds and the transfer can begin immediately. Exclusion
    sizes are -1 (unknown) and total_bytes stays 0."""
    root = Path(root).resolve()
    rules, global_patterns = load_rules(rules_file)
    report = ScanReport(root=root, global_patterns=global_patterns)

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        here = Path(dirpath)
        names = set(filenames)

        excluded_here: set[str] = set()
        for rule in rules:
            marker = next((m for m in rule.markers if m in names), None)
            if marker is None:
                continue
            for dep in rule.exclude:
                if dep in excluded_here or dep not in dirnames:
                    continue
                dep_path = here / dep
                size = _dir_size(dep_path) if compute_sizes else -1
                report.exclusions.append(Exclusion(
                    path=dep_path,
                    rel=dep_path.relative_to(root).as_posix(),
                    rule=rule.name,
                    marker=marker,
                    size=size,
                ))
                if size > 0:
                    report.total_bytes += size
                excluded_here.add(dep)

        if compute_sizes and ".git" in dirnames:
            git_size = _dir_size(here / ".git")
            if git_size >= GIT_REPORT_THRESHOLD:
                rel = (here / ".git").relative_to(root).as_posix()
                report.large_git_dirs.append((rel, git_size))

        # do not descend into excluded dirs (their size is already counted)
        dirnames[:] = [d for d in dirnames if d not in excluded_here]

        if compute_sizes:
            for name in filenames:
                try:
                    report.total_bytes += os.lstat(os.path.join(dirpath, name)).st_size
                    report.file_count += 1
                except OSError:
                    continue
        else:
            report.file_count += len(filenames)

        if on_progress is not None:
            try:
                rel = here.relative_to(root).as_posix()
            except ValueError:
                rel = str(here)
            on_progress(report.file_count, report.total_bytes, rel)

    return report


def _report_cache_path(root: Path) -> Path:
    key = hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:16]
    return SCANS_DIR / f"{key}.json"


def save_report(report: ScanReport) -> None:
    """Persist a scan result for later reuse (PRD F18)."""
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "root": str(report.root),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "file_count": report.file_count,
        "total_bytes": report.total_bytes,
        "global_patterns": report.global_patterns,
        "large_git_dirs": [[r, s] for r, s in report.large_git_dirs],
        "exclusions": [{"rel": e.rel, "rule": e.rule, "marker": e.marker,
                        "size": e.size} for e in report.exclusions],
    }
    _report_cache_path(report.root).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_report(root: Path) -> tuple[ScanReport, str] | None:
    """Load the cached scan for `root`, or None when absent/corrupt.
    Returns (report, scanned_at ISO string)."""
    root = Path(root).resolve()
    try:
        data = json.loads(_report_cache_path(root).read_text(encoding="utf-8"))
        if data.get("root") != str(root):  # hash collision safety net
            return None
        report = ScanReport(
            root=root,
            global_patterns=list(data.get("global_patterns", [])),
            total_bytes=int(data.get("total_bytes", 0)),
            file_count=int(data.get("file_count", 0)),
            large_git_dirs=[(r, int(s)) for r, s in data.get("large_git_dirs", [])],
            exclusions=[Exclusion(path=root / e["rel"], rel=e["rel"],
                                  rule=e.get("rule", ""),
                                  marker=e.get("marker", ""),
                                  size=int(e.get("size", -1)))
                        for e in data.get("exclusions", [])],
        )
        return report, str(data.get("scanned_at", ""))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _escape_filter(rel: str) -> str:
    """Escape rclone filter glob special characters in a literal path."""
    out = []
    for ch in rel:
        if ch in "*?[]{}\\":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def build_filter_lines(report: ScanReport, enabled: set[str] | None = None) -> list[str]:
    """Build rclone --filter-from lines. `enabled` is the set of exclusion
    rel-paths the user confirmed; None means all."""
    lines = ["# generated by lanmigrate"]
    for exc in report.exclusions:
        if enabled is not None and exc.rel not in enabled:
            continue
        lines.append(f"- /{_escape_filter(exc.rel)}/**")
    lines.append("# global junk")
    for pattern in report.global_patterns:
        lines.append(f"- {pattern}")
    return lines

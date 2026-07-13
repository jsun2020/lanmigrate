"""rclone subprocess wrapper (PRD F2 / C.3-1,3).

All rclone parameters are defined in this module only. The baseline flag set
is PRD Appendix B.3 (verified in the M0 manual run). Progress is parsed from
--use-json-log stderr lines, never from plain-text output.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .rclone_bin import ensure_rclone

# Baseline copy flags = PRD B.3. Unattended by design: non-interactive,
# retries handle network blips, --skip-links avoids junction loops.
BASE_COPY_FLAGS = [
    "--transfers", "8",
    "--checkers", "16",
    "--partial-suffix", ".part",
    "--retries", "5",
    "--retries-sleep", "15s",
    "--low-level-retries", "20",
    "--skip-links",
    "--create-empty-src-dirs",
]

STATS_INTERVAL = "2s"


@dataclass
class Progress:
    bytes_done: int = 0
    total_bytes: int = 0
    speed: float = 0.0  # bytes/s
    eta: Optional[int] = None  # seconds
    transfers: int = 0
    total_transfers: int = 0
    errors: int = 0
    current: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.bytes_done * 100.0 / self.total_bytes)


def parse_progress_line(line: str) -> Optional[Progress]:
    """Parse one --use-json-log stderr line. Returns Progress for periodic
    stats lines, None for everything else (regular log messages, non-JSON)."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    stats = obj.get("stats")
    if not isinstance(stats, dict):
        return None
    transferring = stats.get("transferring") or []
    current = transferring[0].get("name", "") if transferring else ""
    eta = stats.get("eta")
    return Progress(
        bytes_done=int(stats.get("bytes", 0)),
        total_bytes=int(stats.get("totalBytes", 0)),
        speed=float(stats.get("speed", 0.0)),
        eta=int(eta) if eta is not None else None,
        transfers=int(stats.get("transfers", 0)),
        total_transfers=int(stats.get("totalTransfers", 0)),
        errors=int(stats.get("errors", 0)),
        current=current,
    )


def obscure(password: str) -> str:
    """rclone connection strings require obscured passwords (PRD B.4-3)."""
    rclone = ensure_rclone()
    out = subprocess.run(
        [str(rclone), "obscure", password],
        capture_output=True, text=True, check=True, encoding="utf-8",
    )
    return out.stdout.strip()


def sftp_remote(host: str, port: int, user: str, obscured_pass: str, dest: str = "/") -> str:
    """Build the on-the-fly sftp remote string (PRD B.3)."""
    if not dest.startswith("/"):
        dest = "/" + dest
    return f":sftp,host={host},port={port},user={user},pass={obscured_pass}:{dest}"


def build_copy_cmd(
    rclone: Path,
    source: Path,
    remote: str,
    filter_file: Optional[Path] = None,
) -> list[str]:
    cmd = [str(rclone), "copy", str(source), remote, *BASE_COPY_FLAGS,
           "--use-json-log", "--log-level", "INFO", "--stats", STATS_INTERVAL]
    if filter_file is not None:
        cmd += ["--filter-from", str(filter_file)]
    return cmd


def run_copy(
    source: Path,
    remote: str,
    filter_file: Optional[Path] = None,
    log_file: Optional[Path] = None,
    on_progress: Optional[Callable[[Progress], None]] = None,
) -> int:
    """Run one rclone copy round. Returns the rclone exit code (0 = every
    file transferred or already up to date). Never raises on rclone failure;
    the caller loops until 0 (PRD F2 unattended loop)."""
    rclone = ensure_rclone()
    cmd = build_copy_cmd(rclone, source, remote, filter_file)
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stderr is not None
        for line in proc.stderr:
            if log_fh:
                log_fh.write(line)
                log_fh.flush()
            progress = parse_progress_line(line)
            if progress and on_progress:
                on_progress(progress)
        return proc.wait()
    finally:
        if log_fh:
            log_fh.close()


def serve_sftp(directory: Path, port: int, user: str, password: str) -> subprocess.Popen:
    """Start the receiver-side SFTP server (plain password; rclone serve
    takes it unobscured). Caller owns the process handle."""
    rclone = ensure_rclone()
    cmd = [
        str(rclone), "serve", "sftp", str(directory),
        "--addr", f":{port}",
        "--user", user,
        "--pass", password,
        "--vfs-cache-mode", "off",
    ]
    return subprocess.Popen(cmd)


def check(source: Path, remote: str, filter_file: Optional[Path] = None) -> int:
    """Post-migration verification (PRD F7, optional strict mode)."""
    rclone = ensure_rclone()
    cmd = [str(rclone), "check", str(source), remote]
    if filter_file is not None:
        cmd += ["--filter-from", str(filter_file)]
    return subprocess.run(cmd).returncode

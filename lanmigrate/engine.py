"""rclone subprocess wrapper (PRD F2 / C.3-1,3).

All rclone parameters are defined in this module only. The baseline flag set
is PRD Appendix B.3 (verified in the M0 manual run). Progress is parsed from
--use-json-log stderr lines, never from plain-text output.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .rclone_bin import ensure_rclone

# Same-name conflict handling (PRD F12). All three are still `rclone copy`:
# receiver-side files are NEVER deleted, identical files are always skipped.
CONFLICT_OVERWRITE = "overwrite"  # source wins where content differs (rclone default)
CONFLICT_UPDATE = "update"        # --update: receiver-newer files are preserved
CONFLICT_KEEP_BOTH = "keep-both"  # --suffix: receiver's version renamed, both kept
CONFLICT_MODES = (CONFLICT_OVERWRITE, CONFLICT_UPDATE, CONFLICT_KEEP_BOTH)

# Set by the GUI sidecar (ipc.py): the app has no console, so child rclone
# processes must not pop console windows of their own.
GUI_MODE = False


def _popen_extra() -> dict:
    if GUI_MODE and os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

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
        **_popen_extra(),
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
    conflict: str = CONFLICT_OVERWRITE,
) -> list[str]:
    """Same-name conflict handling (PRD F9/F12). overwrite = rclone copy
    default (source wins on differing content, identical files skipped);
    update = --update (files newer on the receiver are never overwritten,
    sync mode); keep-both = --suffix (the receiver's differing file is
    renamed name-old-<timestamp>.ext before the source version lands).
    Deletions never propagate either way (rclone copy, not rclone sync)."""
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"unknown conflict mode: {conflict}")
    cmd = [str(rclone), "copy", str(source), remote, *BASE_COPY_FLAGS,
           "--use-json-log", "--log-level", "INFO", "--stats", STATS_INTERVAL]
    if conflict == CONFLICT_UPDATE:
        cmd.append("--update")
    elif conflict == CONFLICT_KEEP_BOTH:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        cmd += ["--suffix", f"-old-{stamp}", "--suffix-keep-extension"]
    if filter_file is not None:
        cmd += ["--filter-from", str(filter_file)]
    return cmd


def run_copy(
    source: Path,
    remote: str,
    filter_file: Optional[Path] = None,
    log_file: Optional[Path] = None,
    on_progress: Optional[Callable[[Progress], None]] = None,
    on_start: Optional[Callable[[subprocess.Popen], None]] = None,
    conflict: str = CONFLICT_OVERWRITE,
) -> int:
    """Run one rclone copy round. Returns the rclone exit code (0 = every
    file transferred or already up to date). Never raises on rclone failure;
    the caller loops until 0 (PRD F2 unattended loop). `on_start` receives
    the Popen handle so a GUI can terminate mid-round."""
    rclone = ensure_rclone()
    cmd = build_copy_cmd(rclone, source, remote, filter_file, conflict=conflict)
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_popen_extra(),
        )
        assert proc.stderr is not None
        if on_start:
            on_start(proc)
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


def serve_sftp(directory: Path, port: int, user: str, password: str,
               capture_log: bool = False) -> subprocess.Popen:
    """Start the receiver-side SFTP server (plain password; rclone serve
    takes it unobscured). Caller owns the process handle. With capture_log
    the server runs at INFO verbosity and stderr is piped; the caller MUST
    keep draining proc.stderr or rclone will block on a full pipe."""
    rclone = ensure_rclone()
    cmd = [
        str(rclone), "serve", "sftp", str(directory),
        "--addr", f":{port}",
        "--user", user,
        "--pass", password,
        "--vfs-cache-mode", "off",
    ]
    if capture_log:
        return subprocess.Popen(cmd + ["-v"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace",
                                **_popen_extra())
    if GUI_MODE:
        # no console to inherit; keep the server quiet and windowless
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **_popen_extra())
    return subprocess.Popen(cmd)


# `rclone serve sftp -v` log lines that reveal transfer activity to the
# receiver (verified empirically against rclone v1.74):
#   ... : serve sftp 10.0.0.2:64735->...: SSH login from u using SSH-2.0-...
#   ... : hello.txt.5a7f61d4.partial: Moved (server-side) to: hello.txt
_SERVE_MOVED_MARK = "Moved (server-side) to: "


def parse_serve_line(line: str) -> Optional[tuple[str, str]]:
    """Classify one serve-sftp stderr line: ("login", client) when a sender
    connects, ("file", name) when a file finishes landing, None otherwise."""
    if "SSH login from" in line:
        return ("login", line.split("SSH login from", 1)[1].strip())
    idx = line.find(_SERVE_MOVED_MARK)
    if idx >= 0:
        name = line[idx + len(_SERVE_MOVED_MARK):].strip()
        if name:
            return ("file", name)
    return None


def list_remote(remote: str, timeout: int = 30) -> list[str]:
    """Top-level entry names at the remote (PRD F12 conflict probe).
    Directory entries come back with a trailing '/', which is stripped.
    Raises RuntimeError on failure - callers decide whether to fail open."""
    rclone = ensure_rclone()
    out = subprocess.run(
        [str(rclone), "lsf", remote, "--max-depth", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, **_popen_extra(),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:] or f"rclone lsf exit {out.returncode}")
    return [line.rstrip("/") for line in out.stdout.splitlines() if line.strip()]


def check(source: Path, remote: str, filter_file: Optional[Path] = None) -> int:
    """Post-migration verification (PRD F7, optional strict mode)."""
    rclone = ensure_rclone()
    cmd = [str(rclone), "check", str(source), remote]
    if filter_file is not None:
        cmd += ["--filter-from", str(filter_file)]
    return subprocess.run(cmd).returncode

"""Locate or download the rclone binary (PRD C.3-7).

Search order: LANMIGRATE_RCLONE env var -> bundled (PyInstaller) -> PATH ->
~/.lanmigrate/bin -> download from downloads.rclone.org.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DOWNLOAD_TIMEOUT = 180  # seconds
BASE_URL = "https://downloads.rclone.org"


def _exe_name() -> str:
    return "rclone.exe" if os.name == "nt" else "rclone"


def default_bin_dir() -> Path:
    return Path.home() / ".lanmigrate" / "bin"


def _bundled_dir() -> Path | None:
    # PyInstaller one-file bundles extract to sys._MEIPASS
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "bin" if base else None


def find_rclone() -> Path | None:
    env = os.environ.get("LANMIGRATE_RCLONE")
    if env and Path(env).is_file():
        return Path(env)
    bundled = _bundled_dir()
    if bundled and (bundled / _exe_name()).is_file():
        return bundled / _exe_name()
    which = shutil.which("rclone")
    if which:
        return Path(which)
    local = default_bin_dir() / _exe_name()
    if local.is_file():
        return local
    return None


def _platform_tag() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "Windows":
        return f"windows-{arch}"
    if system == "Darwin":
        return f"osx-{arch}"
    return f"linux-{arch}"


def download_rclone(dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or default_bin_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"{BASE_URL}/rclone-current-{_platform_tag()}.zip"
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "rclone.zip"
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp, open(zip_path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with zipfile.ZipFile(zip_path) as zf:
            member = next(n for n in zf.namelist() if n.endswith(_exe_name()))
            zf.extract(member, td)
        target = dest_dir / _exe_name()
        shutil.copyfile(Path(td) / member, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def ensure_rclone(auto_download: bool = True) -> Path:
    found = find_rclone()
    if found:
        return found
    if not auto_download:
        raise FileNotFoundError(
            "rclone not found. Install it, or set LANMIGRATE_RCLONE to the binary path."
        )
    return download_rclone()

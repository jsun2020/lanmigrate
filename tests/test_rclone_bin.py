"""Bundled-rclone resolution (PRD F10: zero-download startup)."""
from __future__ import annotations

import sys
from pathlib import Path

from lanmigrate import rclone_bin


def _fake_exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake rclone")


def test_bundled_rclone_installed_to_stable_path(tmp_path, monkeypatch):
    """A bundled copy is installed once to ~/.lanmigrate/bin and resolved
    from there (stable path keeps firewall rules valid across launches)."""
    meipass = tmp_path / "meipass"
    _fake_exe(meipass / "bin" / rclone_bin._exe_name())
    bin_dir = tmp_path / "home-bin"
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(rclone_bin, "default_bin_dir", lambda: bin_dir)
    monkeypatch.delenv("LANMIGRATE_RCLONE", raising=False)

    found = rclone_bin.find_rclone()

    assert found == bin_dir / rclone_bin._exe_name()
    assert found.read_bytes() == b"MZ fake rclone"


def test_bundled_install_skips_existing_copy(tmp_path, monkeypatch):
    """An already-installed copy is never overwritten (user may have
    updated it manually)."""
    meipass = tmp_path / "meipass"
    _fake_exe(meipass / "bin" / rclone_bin._exe_name())
    bin_dir = tmp_path / "home-bin"
    existing = bin_dir / rclone_bin._exe_name()
    _fake_exe(existing)
    existing.write_bytes(b"existing copy")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(rclone_bin, "default_bin_dir", lambda: bin_dir)
    monkeypatch.delenv("LANMIGRATE_RCLONE", raising=False)

    found = rclone_bin.find_rclone()

    assert found == existing
    assert existing.read_bytes() == b"existing copy"


def test_env_var_beats_bundled(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    _fake_exe(meipass / "bin" / rclone_bin._exe_name())
    override = tmp_path / "custom" / rclone_bin._exe_name()
    _fake_exe(override)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv("LANMIGRATE_RCLONE", str(override))

    assert rclone_bin.find_rclone() == override


def test_no_bundled_dir_outside_frozen_build(monkeypatch, tmp_path):
    """Source runs (no _MEIPASS) skip the bundled step entirely."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert rclone_bin._install_bundled() is None

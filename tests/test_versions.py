"""Every version declaration in the repo must match.

Born from a real slip: v0.5/v0.6 bumped Cargo.toml but nobody regenerated
Cargo.lock, so the repo advertised lanmigrate-gui 0.4.0 until a user
noticed. This test makes any future drift a loud CI failure.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from lanmigrate import __version__

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    # normalize CRLF so multi-line regexes behave (Windows checkouts)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_all_version_declarations_match():
    pyproject = re.search(r'^version = "(.+)"', _read("pyproject.toml"), re.M).group(1)
    tauri_conf = json.loads(_read("gui/src-tauri/tauri.conf.json"))["version"]
    cargo_toml = re.search(r'^version = "(.+)"', _read("gui/src-tauri/Cargo.toml"), re.M).group(1)
    cargo_lock = re.search(r'name = "lanmigrate-gui"\nversion = "(.+)"',
                           _read("gui/src-tauri/Cargo.lock")).group(1)

    versions = {
        "lanmigrate/__init__.py": __version__,
        "pyproject.toml": pyproject,
        "gui/src-tauri/tauri.conf.json": tauri_conf,
        "gui/src-tauri/Cargo.toml": cargo_toml,
        "gui/src-tauri/Cargo.lock (lanmigrate-gui)": cargo_lock,
    }
    assert len(set(versions.values())) == 1, (
        f"version drift: {versions} - bump every file and run "
        f"`cargo update --workspace` in gui/src-tauri to refresh the lockfile"
    )

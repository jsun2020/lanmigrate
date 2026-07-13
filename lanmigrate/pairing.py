"""Pairing codes, device fingerprints, and session credentials (PRD F1).

The 6-digit pairing code shown on the receiver is the only secret the user
types. Both sides derive the SFTP session password from it, so it never
travels over the network. Deriving from the code alone (not code+fingerprint)
keeps the manual IP:port fallback working when mDNS is blocked.

Paired devices are remembered by fingerprint in ~/.lanmigrate/devices.json so
reconnecting after an IP/Wi-Fi change needs no new pairing (PRD F1).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".lanmigrate"
DEVICE_ID_FILE = CONFIG_DIR / "device_id"
DEVICES_FILE = CONFIG_DIR / "devices.json"

SFTP_USER = "lanmigrate"


def generate_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def session_password(code: str) -> str:
    digest = hashlib.sha256(f"lanmigrate-v1:{code}".encode()).hexdigest()
    return digest[:20]


def device_fingerprint() -> str:
    """Stable identity for this machine, generated once and persisted."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if DEVICE_ID_FILE.is_file():
        secret = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
    else:
        secret = secrets.token_hex(32)
        DEVICE_ID_FILE.write_text(secret, encoding="utf-8")
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _load_devices() -> dict:
    if not DEVICES_FILE.is_file():
        return {}
    try:
        return json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_device(fp: str, name: str, code: str) -> None:
    """Persist a paired receiver so future runs can reconnect without a code."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    devices = _load_devices()
    devices[fp] = {"name": name, "code": code}
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(devices, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, DEVICES_FILE)


def recall_device(fp: str) -> dict | None:
    return _load_devices().get(fp)

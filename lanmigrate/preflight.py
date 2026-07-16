"""Startup environment self-check (PRD F11).

LanMigrate itself never needs administrator rights: rclone lives under the
user profile and the SFTP port (default 2022) is above the privileged range,
so binding it is a plain user-space operation. The single thing elevation
unlocks is the Windows Firewall inbound rule for the RECEIVER - without a
rule, Windows silently drops the sender's incoming connection. The sender
side only makes outbound connections and is never affected.

This module answers three questions at startup:
  1. is rclone available without any download? (bundled / installed / PATH)
  2. is the current user elevated?
  3. is the receive port free, and does a firewall allow-rule exist?
and, when elevated, creates the firewall rule automatically.
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
from pathlib import Path

from . import engine, rclone_bin

FIREWALL_RULE_PREFIX = "LanMigrate"


def is_admin() -> bool:
    """Elevated (Windows) or root (POSIX). Errors mean 'assume standard
    user' - the check only gates optional conveniences."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def port_available(port: int) -> bool:
    """Can this user bind the receive port right now?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", port))
        return True
    except OSError:
        return False


def rclone_source() -> tuple[str, Path | None]:
    """Where would rclone come from? 'env' | 'local' (~/.lanmigrate/bin,
    includes the bundled copy installed there) | 'path' | 'missing'
    ('missing' means a first-use download would be required)."""
    found = rclone_bin.find_rclone()
    if found is None:
        return "missing", None
    env = os.environ.get("LANMIGRATE_RCLONE")
    if env and Path(env) == found:
        return "env", found
    if found.parent == rclone_bin.default_bin_dir():
        return "local", found
    return "path", found


# ------------------------------------------------------------- firewall


def _rule_name(port: int) -> str:
    return f"{FIREWALL_RULE_PREFIX}-{port}"


def _netsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["netsh", "advfirewall", "firewall", *args],
        capture_output=True, text=True, errors="replace",
        **engine._popen_extra(),
    )


def firewall_rule_exists(port: int) -> bool:
    """True when an inbound allow-rule for the port exists (or the OS has
    no Windows Firewall). `netsh show rule` exits non-zero when absent."""
    if os.name != "nt":
        return True
    return _netsh("show", "rule", f"name={_rule_name(port)}").returncode == 0


def ensure_firewall_rule(port: int) -> tuple[bool, str]:
    """Create the port-scoped inbound allow rule (idempotent). Needs
    elevation; callers check is_admin() first. Port-scoped (not
    program-scoped) so it stays valid whichever rclone copy serves."""
    if os.name != "nt":
        return True, "non-Windows: firewall rule not needed"
    if firewall_rule_exists(port):
        return True, f"防火墙放行规则已存在 ({_rule_name(port)})"
    out = _netsh(
        "add", "rule", f"name={_rule_name(port)}",
        "dir=in", "action=allow", "protocol=TCP", f"localport={port}",
    )
    if out.returncode == 0:
        return True, f"已自动添加防火墙放行规则 ({_rule_name(port)})"
    detail = (out.stdout + out.stderr).strip()
    return False, f"添加防火墙规则失败: {detail[:200]}"


# ------------------------------------------------------------- report


def check(port: int = 2022) -> dict:
    """Full startup self-check. Cheap (one netsh + one bind probe on
    Windows); safe to run on every launch."""
    source, path = rclone_source()
    return {
        "admin": is_admin(),
        "rclone": str(path) if path else None,
        "rclone_source": source,
        "port": port,
        "port_free": port_available(port),
        "firewall_rule": firewall_rule_exists(port),
    }
